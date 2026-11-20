"""Prepare one exact sender change. Does not import, publish or execute n8n."""
import copy

SENDER_ID = "GABMailingFix20261116V2"


def raise_attachment_limit(workflow):
    if workflow.get("id") != SENDER_ID:
        raise ValueError("Refusing to modify another workflow")
    nodes = workflow.get("nodes") or []
    if len(nodes) != 5 or sum(n.get("type") == "n8n-nodes-base.emailSend" for n in nodes) != 1:
        raise ValueError("Sender topology changed; review required")
    result = copy.deepcopy(workflow)
    prepare = [n for n in result["nodes"] if n.get("name") == "Prepare Mail" and n.get("type") == "n8n-nodes-base.code"]
    if len(prepare) != 1:
        raise ValueError("Prepare Mail node is missing or ambiguous")
    code = prepare[0]["parameters"].get("jsCode", "")
    old = "totalBytes > 8 * 1024 * 1024"
    new = "totalBytes > 12 * 1024 * 1024"
    if code.count(old) != 1 or code.count("Общий размер вложений превышает 8 МБ") != 1:
        raise ValueError("Original size guard changed; review required")
    prepare[0]["parameters"]["jsCode"] = code.replace(old, new).replace("Общий размер вложений превышает 8 МБ", "Общий размер вложений превышает 12 МиБ")
    return result
