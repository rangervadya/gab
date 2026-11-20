"""Synthetic size checks only; no SMTP, webhook or production database."""
import base64
import unittest
from app.mailing_payload import encode_payload, validate_planned_payload, MAX_WEBHOOK_BYTES


class PayloadTests(unittest.TestCase):
    def test_original_sized_bundle_fits(self):
        sizes = [1637136, 3113188, 4261971, 2331681, 245145]
        attachments = [dict(original_filename=f"synthetic-{i}.pdf", mime_type="application/pdf", size_bytes=n)
                       for i, n in enumerate(sizes)]
        size = validate_planned_payload(6, "Synthetic subject", "<p>Text</p>", "Text", attachments, ["one@example.test"])
        self.assertLess(size, MAX_WEBHOOK_BYTES)
        self.assertGreater(size, sum(sizes))

    def test_estimate_matches_actual_utf8_and_base64(self):
        data = b"synthetic" * 101
        metadata = [dict(original_filename="Пример.pdf", mime_type="application/pdf", size_bytes=len(data))]
        estimate = validate_planned_payload(6, "Тема", "<p>Текст</p>", "Текст", metadata, ["one@example.test"])
        payload = dict(request_id="0" * 36, mailing_id=6, to_email="one@example.test", subject="Тема",
                       body_html="<p>Текст</p>", body_text="Текст",
                       attachments=[dict(filename="Пример.pdf", mime_type="application/pdf", data_base64=base64.b64encode(data).decode())])
        self.assertEqual(estimate, len(encode_payload(payload)))

    def test_base64_overhead_rejected_before_dispatch(self):
        with self.assertRaises(ValueError):
            validate_planned_payload(6, "Subject", "Text", "", [dict(original_filename="x.pdf", mime_type="application/pdf", size_bytes=12*1024*1024)], ["one@example.test"])

    def test_encoded_body_limit(self):
        with self.assertRaises(ValueError):
            encode_payload({"body_text": "x" * MAX_WEBHOOK_BYTES})

    def test_raw_attachment_limit(self):
        with self.assertRaises(ValueError):
            validate_planned_payload(6, "Subject", "", "Text", [dict(original_filename="x.pdf", mime_type="application/pdf", size_bytes=13*1024*1024)], [])
