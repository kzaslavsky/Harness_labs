"""Tests for bounded, policy-controlled child attempts."""

from __future__ import annotations

import threading
import time
import unittest

from harness_labs import (
    ChildAuthorization,
    ChildBatchRequest,
    ChildDispatcher,
    ChildRequest,
    ChildRequestDenied,
    TaskAttempt,
    TaskResult,
)


class RecordingExecutor:
    def __init__(self) -> None:
        self.attempts: list[TaskAttempt] = []
        self.messages: list[str] = []
        self.closed = False

    def execute(self, attempt: TaskAttempt) -> TaskResult:
        self.attempts.append(attempt)
        return TaskResult(
            attempt_id=attempt.attempt_id,
            status="succeeded",
            payload={"text": "there is booty here"},
            evidence=("file:sha256:abc",),
        )

    def send(self, attempt: TaskAttempt, message: str) -> TaskResult:
        self.messages.append(message)
        return TaskResult(
            attempt_id=attempt.attempt_id,
            status="succeeded",
            payload={"text": "The read_file capability enabled me."},
            evidence=("model-invocation:follow-up",),
        )

    def close(self) -> None:
        self.closed = True


class ParallelProbe:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.barrier = threading.Barrier(2)
        self.active = 0
        self.maximum_active = 0
        self.calls = 0


class ProbeExecutor:
    def __init__(
        self,
        probe: ParallelProbe,
        text: str,
        *,
        delay: float = 0.0,
        error: Exception | None = None,
    ) -> None:
        self.probe = probe
        self.text = text
        self.delay = delay
        self.error = error

    def execute(self, attempt: TaskAttempt) -> TaskResult:
        with self.probe.lock:
            self.probe.calls += 1
            call_number = self.probe.calls
            self.probe.active += 1
            self.probe.maximum_active = max(
                self.probe.maximum_active,
                self.probe.active,
            )
        try:
            if call_number <= 2:
                self.probe.barrier.wait(timeout=1)
            if self.delay:
                time.sleep(self.delay)
            if self.error is not None:
                raise self.error
            return TaskResult(
                attempt_id=attempt.attempt_id,
                status="succeeded",
                payload={"text": self.text},
            )
        finally:
            with self.probe.lock:
                self.probe.active -= 1


class GateExecutor:
    def __init__(self, started: threading.Event, release: threading.Event) -> None:
        self.started = started
        self.release = release

    def execute(self, attempt: TaskAttempt) -> TaskResult:
        self.started.set()
        if not self.release.wait(timeout=2):
            raise RuntimeError("test gate timed out")
        return TaskResult(attempt.attempt_id, "succeeded")


class ChildDispatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = TaskAttempt(
            attempt_id="treasure",
            task_ref="task:parent",
            context_ref="context:parent",
            grant_ref="grant:spawn-reader",
        )
        self.reader = RecordingExecutor()
        self.dispatcher = ChildDispatcher(
            self.root,
            {
                "file_reader": ChildAuthorization(
                    role="file_reader",
                    task_ref="task:read-treasure",
                    context_ref="context:treasure",
                    grant_ref="grant:read-treasure",
                    backend_id="recording",
                    capabilities=frozenset({"read_file"}),
                    executor=self.reader,
                )
            },
        )

    def test_policy_constructs_and_runs_child_attempt(self) -> None:
        result = self.dispatcher.run_child(
            self.root,
            ChildRequest(
                role="file_reader",
                objective="Read treasure_chest.txt",
            ),
        )

        child = self.reader.attempts[0]
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(child.parent_attempt_id, self.root.attempt_id)
        self.assertEqual(child.task_ref, "task:read-treasure")
        self.assertEqual(child.grant_ref, "grant:read-treasure")
        self.assertEqual(
            [event.event_type for event in self.dispatcher.events],
            ["child_dispatched", "child_completed"],
        )
        self.assertEqual(
            self.dispatcher.events[-1].evidence,
            ("file:sha256:abc",),
        )
        self.assertEqual(self.dispatcher.events[0].backend_id, "recording")
        self.assertEqual(
            self.dispatcher.events[0].capabilities,
            ("read_file",),
        )

    def test_parent_cannot_select_an_unauthorized_role(self) -> None:
        with self.assertRaisesRegex(ChildRequestDenied, "not authorized"):
            self.dispatcher.run_child(
                self.root,
                ChildRequest(role="arbitrary_shell", objective="Read everything"),
            )

        self.assertEqual(self.reader.attempts, [])
        self.assertEqual(self.dispatcher.events, ())

    def test_child_count_is_bounded(self) -> None:
        request = ChildRequest(role="file_reader", objective="Read the treasure")
        self.dispatcher.run_child(self.root, request)

        with self.assertRaisesRegex(ChildRequestDenied, "maximum children"):
            self.dispatcher.run_child(self.root, request)

        self.assertEqual(len(self.reader.attempts), 1)

    def test_child_depth_is_bounded(self) -> None:
        request = ChildRequest(role="file_reader", objective="Read the treasure")
        self.dispatcher.run_child(self.root, request)
        child = self.reader.attempts[0]

        with self.assertRaisesRegex(ChildRequestDenied, "maximum child depth"):
            self.dispatcher.run_child(child, request)

        self.assertEqual(len(self.reader.attempts), 1)

    def test_unregistered_parent_cannot_spawn(self) -> None:
        imposter = TaskAttempt(
            attempt_id=self.root.attempt_id,
            task_ref="task:different",
            context_ref=self.root.context_ref,
            grant_ref=self.root.grant_ref,
        )

        with self.assertRaisesRegex(ChildRequestDenied, "not registered"):
            self.dispatcher.run_child(
                imposter,
                ChildRequest(role="file_reader", objective="Read the treasure"),
            )

    def test_retained_child_receives_message_then_terminates(self) -> None:
        initial = self.dispatcher.start_child(
            self.root,
            ChildRequest(role="file_reader", objective="Read the treasure"),
            keep_alive=True,
        )
        followup = self.dispatcher.send_child_message(
            self.root,
            initial.attempt_id,
            "what enabled you to answer me this way?",
        )
        self.dispatcher.terminate_child(self.root, initial.attempt_id)

        self.assertEqual(followup.status, "succeeded")
        self.assertEqual(
            self.reader.messages,
            ["what enabled you to answer me this way?"],
        )
        self.assertTrue(self.reader.closed)
        self.assertEqual(
            [event.event_type for event in self.dispatcher.events],
            [
                "child_dispatched",
                "child_responded",
                "child_message_sent",
                "child_responded",
                "child_terminated",
            ],
        )

    def test_batch_runs_concurrently_but_returns_submission_order(self) -> None:
        probe = ParallelProbe()
        roles = ("slow", "medium", "fast")
        executors = {
            "slow": ProbeExecutor(probe, "slow", delay=0.03),
            "medium": ProbeExecutor(probe, "medium", delay=0.01),
            "fast": ProbeExecutor(probe, "fast"),
        }
        dispatcher = ChildDispatcher(
            self.root,
            {
                role: ChildAuthorization(
                    role=role,
                    task_ref=f"task:{role}",
                    context_ref=f"context:{role}",
                    grant_ref=f"grant:{role}",
                    backend_id="probe",
                    capabilities=frozenset({"inspect"}),
                    executor=executors[role],
                )
                for role in roles
            },
            max_children_per_attempt=3,
        )

        batch = dispatcher.run_children(
            self.root,
            ChildBatchRequest(
                requests=tuple(
                    ChildRequest(role=role, objective=f"Inspect {role}")
                    for role in roles
                ),
                max_parallelism=2,
            ),
        )

        self.assertTrue(batch.succeeded)
        self.assertEqual(probe.maximum_active, 2)
        self.assertEqual(
            [result.payload["text"] for result in batch.results],
            list(roles),
        )
        self.assertEqual(
            [result.attempt_id for result in batch.results],
            ["treasure/child-1", "treasure/child-2", "treasure/child-3"],
        )

    def test_batch_collects_executor_failure_without_cancelling_peers(self) -> None:
        probe = ParallelProbe()
        dispatcher = ChildDispatcher(
            self.root,
            {
                role: ChildAuthorization(
                    role=role,
                    task_ref=f"task:{role}",
                    context_ref=f"context:{role}",
                    grant_ref=f"grant:{role}",
                    backend_id="probe",
                    capabilities=frozenset({"inspect"}),
                    executor=ProbeExecutor(
                        probe,
                        role,
                        error=RuntimeError("broken child") if role == "bad" else None,
                    ),
                )
                for role in ("good", "bad")
            },
            max_children_per_attempt=2,
        )

        batch = dispatcher.run_children(
            self.root,
            ChildBatchRequest(
                requests=(
                    ChildRequest("good", "Inspect good"),
                    ChildRequest("bad", "Inspect bad"),
                ),
                max_parallelism=2,
            ),
        )

        self.assertFalse(batch.succeeded)
        self.assertEqual(
            [result.status for result in batch.results],
            ["succeeded", "failed"],
        )
        self.assertEqual(batch.results[1].payload["error"], "broken child")
        self.assertEqual(probe.calls, 2)

    def test_single_child_preserves_executor_exception_semantics(self) -> None:
        class BrokenExecutor:
            def execute(self, attempt: TaskAttempt) -> TaskResult:
                raise RuntimeError("single child broke")

        dispatcher = ChildDispatcher(
            self.root,
            {
                "broken": ChildAuthorization(
                    role="broken",
                    task_ref="task:broken",
                    context_ref="context:broken",
                    grant_ref="grant:broken",
                    backend_id="broken",
                    capabilities=frozenset(),
                    executor=BrokenExecutor(),
                )
            },
        )

        with self.assertRaisesRegex(RuntimeError, "single child broke"):
            dispatcher.run_child(
                self.root,
                ChildRequest("broken", "Break"),
            )

    def test_batch_is_fully_validated_before_any_child_launches(self) -> None:
        dispatcher = ChildDispatcher(
            self.root,
            {
                "file_reader": self.dispatcher._authorizations["file_reader"],
            },
            max_children_per_attempt=2,
        )

        with self.assertRaisesRegex(ChildRequestDenied, "not authorized"):
            dispatcher.run_children(
                self.root,
                ChildBatchRequest(
                    requests=(
                        ChildRequest("file_reader", "Read"),
                        ChildRequest("not_allowed", "Escape"),
                    ),
                    max_parallelism=2,
                ),
            )

        self.assertEqual(self.reader.attempts, [])
        self.assertEqual(dispatcher.events, ())

    def test_concurrent_batches_receive_distinct_batch_ids(self) -> None:
        release = threading.Event()
        started = {"a": threading.Event(), "b": threading.Event()}
        dispatcher = ChildDispatcher(
            self.root,
            {
                role: ChildAuthorization(
                    role=role,
                    task_ref=f"task:{role}",
                    context_ref=f"context:{role}",
                    grant_ref=f"grant:{role}",
                    backend_id="gate",
                    capabilities=frozenset(),
                    executor=GateExecutor(started[role], release),
                )
                for role in ("a", "b")
            },
            max_children_per_attempt=2,
        )
        results = []

        def run(role: str) -> None:
            results.append(
                dispatcher.run_children(
                    self.root,
                    ChildBatchRequest(
                        requests=(ChildRequest(role, f"Run {role}"),),
                        max_parallelism=1,
                    ),
                )
            )

        threads = [
            threading.Thread(target=run, args=(role,)) for role in ("a", "b")
        ]
        for thread in threads:
            thread.start()
        self.assertTrue(started["a"].wait(timeout=1))
        self.assertTrue(started["b"].wait(timeout=1))
        release.set()
        for thread in threads:
            thread.join(timeout=2)

        self.assertEqual(
            {result.batch_id for result in results},
            {"treasure/batch-1", "treasure/batch-2"},
        )

    def test_inflight_role_cannot_reuse_stateful_executor(self) -> None:
        started = threading.Event()
        release = threading.Event()
        dispatcher = ChildDispatcher(
            self.root,
            {
                "shared": ChildAuthorization(
                    role="shared",
                    task_ref="task:shared",
                    context_ref="context:shared",
                    grant_ref="grant:shared",
                    backend_id="gate",
                    capabilities=frozenset(),
                    executor=GateExecutor(started, release),
                )
            },
            max_children_per_attempt=2,
        )
        thread = threading.Thread(
            target=lambda: dispatcher.run_child(
                self.root,
                ChildRequest("shared", "First"),
            )
        )
        thread.start()
        self.assertTrue(started.wait(timeout=1))

        with self.assertRaisesRegex(ChildRequestDenied, "in-flight executor"):
            dispatcher.run_child(
                self.root,
                ChildRequest("shared", "Second"),
            )

        release.set()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
