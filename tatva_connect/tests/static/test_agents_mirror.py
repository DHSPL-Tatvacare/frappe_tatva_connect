# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""CLAUDE.md and AGENTS.md are one document in two files, so any difference between them is a defect. Both are local-only, so CI skips. No bench, no frappe."""
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_CLAUDE, _AGENTS = _ROOT / "CLAUDE.md", _ROOT / "AGENTS.md"


@unittest.skipUnless(_CLAUDE.exists() and _AGENTS.exists(), "CLAUDE.md and AGENTS.md are local-only")
class TestAgentsMirror(unittest.TestCase):
	def test_claude_md_and_agents_md_are_identical(self):
		self.assertEqual(_CLAUDE.read_text(encoding="utf-8"), _AGENTS.read_text(encoding="utf-8"),
			"CLAUDE.md and AGENTS.md differ: update both in the same change")
