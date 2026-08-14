from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from harness_labs.core.audit import AuditActor, AuditJournal
from harness_labs.core.usage import ModelPrice, parse_codex_jsonl_usage, usage_payload


class UsageTests(unittest.TestCase):
    def test_codex_usage_is_priced_and_summarized(self) -> None:
        raw = (
            '{"type":"turn.completed","usage":{"input_tokens":1000,'
            '"cached_input_tokens":400,"output_tokens":100}}\n'
        )
        parsed = parse_codex_jsonl_usage(raw)
        self.assertIsNotNone(parsed)
        price = ModelPrice(
            "model-x",
            Decimal("2"),
            Decimal("0.5"),
            Decimal("8"),
            "test-price/1",
        )
        usage = usage_payload(model="model-x", pricing=price, **parsed)
        self.assertEqual(usage["cost_usd"], "0.002200")
        with tempfile.TemporaryDirectory() as temporary:
            audit = AuditJournal(
                Path(temporary) / "run",
                "usage-test",
                actor=AuditActor("kernel", "controller"),
                evidence_classification="component",
            )
            audit.append(
                "backend_transport",
                status="succeeded",
                payload={"usage": usage, "tool_calls": 3},
                backend_id="model-x-backend",
                duration_ms=25,
            )
            audit.finalize("succeeded", result={"status": "succeeded"})
            summary = json.loads(audit.summary_path.read_text())
            self.assertEqual(summary["usage"]["input_tokens"], 1000)
            self.assertEqual(summary["usage"]["cached_input_tokens"], 400)
            self.assertEqual(summary["usage"]["cost_usd"], "0.002200")
            self.assertTrue(summary["usage"]["cost_complete"])


if __name__ == "__main__":
    unittest.main()
