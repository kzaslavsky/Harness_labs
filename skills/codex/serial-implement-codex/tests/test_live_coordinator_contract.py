from __future__ import annotations

import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).parents[1]
SERIAL_SKILL = SKILL_ROOT / "SKILL.md"
SERIAL_PROTOCOL = SKILL_ROOT / "references" / "protocol.md"
IMPLEMENT_ROOT = SKILL_ROOT.parent / "implement-v13-codex"


class LiveCoordinatorContractTests(unittest.TestCase):
    def test_serial_uses_executable_controller_before_passive_observation(self) -> None:
        skill = SERIAL_SKILL.read_text(encoding="utf-8")
        controller = skill.index("run_feature.py DISPATCH.json")
        observation = skill.index("External monitoring uses")
        self.assertLess(controller, observation)
        self.assertIn("Do not spawn an app-task coordinator", skill)
        self.assertIn("does not own lifecycle progress", skill)

    def test_protocol_assigns_terminal_state_to_controller(self) -> None:
        serial_protocol = SERIAL_PROTOCOL.read_text(encoding="utf-8")
        implement_skill = (IMPLEMENT_ROOT / "SKILL.md").read_text(encoding="utf-8")
        implement_protocol = (IMPLEMENT_ROOT / "references" / "protocol.md").read_text(
            encoding="utf-8"
        )
        for document in (serial_protocol, implement_skill, implement_protocol):
            self.assertIn("run_feature.py", document)
        self.assertIn("atomically settles the queue", serial_protocol)
        self.assertIn("App-task messaging is observational", implement_protocol)


if __name__ == "__main__":
    unittest.main()
