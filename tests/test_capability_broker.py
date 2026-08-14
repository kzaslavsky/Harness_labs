from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness_labs.core.audit import AuditActor, AuditJournal
from harness_labs.core.capability_broker import (
    BrokeredCapabilityExecutor,
    CapabilityBroker,
    CapabilityDenied,
    CapabilityPolicy,
    CapabilityRequest,
)
from harness_labs.core.attempts import TaskAttempt
from harness_labs.core.controller_evidence import EvidenceCatalog
from harness_labs.core.controller_results import validate_semantic_result


class CapabilityBrokerTests(unittest.TestCase):
    def test_allowlists_audit_and_idempotent_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            audit = AuditJournal(
                Path(temporary) / "run",
                "broker-test",
                actor=AuditActor("kernel", "controller"),
                evidence_classification="component",
            )
            calls = []
            broker = CapabilityBroker(
                CapabilityPolicy(
                    browser_operations=frozenset({"inspect"}),
                    browser_origins=frozenset({"http://127.0.0.1:8765"}),
                ),
                {"browser": lambda request: calls.append(request.target) or {"ok": True}},
                audit=audit,
            )
            request = CapabilityRequest(
                "request-1",
                "browser",
                "inspect",
                "http://127.0.0.1:8765/page",
                {},
                "same-effect",
            )
            first = broker.execute(request)
            second = broker.execute(request)
            self.assertEqual(first.status, "succeeded")
            self.assertTrue(second.replayed)
            self.assertEqual(calls, ["http://127.0.0.1:8765/page"])
            audit.finalize("succeeded", result={"status": "succeeded"})
            summary = json.loads(audit.summary_path.read_text())
            self.assertEqual(summary["usage"]["tool_calls"], 1)

    def test_network_and_external_effects_fail_closed(self) -> None:
        broker = CapabilityBroker(
            CapabilityPolicy(
                network_operations=frozenset({"GET"}),
                network_hosts=frozenset({"api.example.test"}),
                external_effect_operations=frozenset({"send"}),
                external_effect_targets=frozenset({"mailbox:test"}),
            ),
            {"network": lambda request: {}, "external_effect": lambda request: {}},
        )
        with self.assertRaises(CapabilityDenied):
            broker.execute(
                CapabilityRequest(
                    "bad-host",
                    "network",
                    "GET",
                    "https://other.example.test/data",
                    {},
                    "bad-host",
                )
            )
        with self.assertRaises(CapabilityDenied):
            broker.execute(
                CapabilityRequest(
                    "missing-auth",
                    "external_effect",
                    "send",
                    "mailbox:test",
                    {},
                    "send-once",
                )
            )

    def test_broker_is_usable_as_a_capability_scheduled_executor(self) -> None:
        evidence = EvidenceCatalog()
        broker = CapabilityBroker(
            CapabilityPolicy(
                network_operations=frozenset({"GET"}),
                network_hosts=frozenset({"api.example.test"}),
            ),
            {"network": lambda request: {"status_code": 200}},
        )
        request = {
            "protocol": "capability-request/1",
            "request_id": "lookup-1",
            "kind": "network",
            "operation": "GET",
            "target": "https://api.example.test/data",
            "payload": {},
            "idempotency_key": "lookup-once",
            "authorization_ref": None,
        }
        task = {
            "id": "network-task",
            "context": json.dumps({"capability_request": request}),
            "required_capabilities": ["network"],
            "details_schema": "network-result/1",
        }
        result = BrokeredCapabilityExecutor(task, broker, evidence).execute(
            TaskAttempt(
                "network-task/attempt-1",
                "task:network-task",
                "context:network-task",
                "profile:network",
            )
        )
        semantic = validate_semantic_result(
            result, expected_details_schema="network-result/1"
        )
        self.assertEqual(
            semantic.details["capability_receipt"]["result"]["status_code"],
            200,
        )


if __name__ == "__main__":
    unittest.main()
