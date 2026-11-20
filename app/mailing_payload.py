"""Shared size limits; validation never performs network I/O."""
import json

MAX_ATTACHMENTS_BYTES = 12 * 1024 * 1024
MAX_WEBHOOK_BYTES = 16 * 1024 * 1024


def encode_payload(payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_WEBHOOK_BYTES:
        raise ValueError("Письмо с вложениями слишком большое: уменьшите размер файлов или текста")
    return body


def validate_planned_payload(mailing_id, subject, body_html, body_text, attachments, emails):
    """Account for Base64 and UTF-8 before a campaign can be launched."""
    total = sum(int(item["size_bytes"]) for item in attachments)
    if total > MAX_ATTACHMENTS_BYTES:
        raise ValueError("Общий размер вложений превышает 12 МиБ")
    skeleton = {
        "request_id": "0" * 36,
        "mailing_id": mailing_id,
        "to_email": max(emails or [""], key=lambda s: len(json.dumps(s, ensure_ascii=False).encode())),
        "subject": subject,
        "body_html": body_html,
        "body_text": body_text,
        "attachments": [{"filename": a["original_filename"], "mime_type": a["mime_type"], "data_base64": ""} for a in attachments],
    }
    size = len(encode_payload(skeleton)) + sum(4 * ((int(a["size_bytes"]) + 2) // 3) for a in attachments)
    if size > MAX_WEBHOOK_BYTES:
        raise ValueError("Письмо с вложениями слишком большое: уменьшите размер файлов или текста")
    return size
