import unittest
from scripts.mailing_limit import raise_attachment_limit, SENDER_ID


class SenderLimitTests(unittest.TestCase):
    def fixture(self):
        return {"id": SENDER_ID, "connections": {"unchanged": {}}, "nodes": [
            {"name": "Prepare Mail", "type": "n8n-nodes-base.code", "parameters": {
                "jsCode": "if (totalBytes > 8 * 1024 * 1024) { throw new Error('Общий размер вложений превышает 8 МБ'); }"}},
            {"type": "n8n-nodes-base.emailSend", "credentials": {"smtp": {"id": "synthetic"}}},
            {"type": "n8n-nodes-base.webhook", "parameters": {"path": "synthetic-do-not-call"}},
            {"type": "n8n-nodes-base.code"}, {"type": "n8n-nodes-base.respondToWebhook"}]}

    def test_only_size_guard_changes(self):
        source = self.fixture()
        changed = raise_attachment_limit(source)
        self.assertIn("12 * 1024", changed["nodes"][0]["parameters"]["jsCode"])
        self.assertIn("8 * 1024", source["nodes"][0]["parameters"]["jsCode"])
        self.assertEqual(source["nodes"][1:], changed["nodes"][1:])
        self.assertEqual(source["connections"], changed["connections"])

    def test_other_workflow_rejected(self):
        source = self.fixture(); source["id"] = "another-workflow"
        with self.assertRaises(ValueError): raise_attachment_limit(source)

    def test_changed_sender_requires_review(self):
        source = self.fixture(); source["nodes"].pop()
        with self.assertRaises(ValueError): raise_attachment_limit(source)
