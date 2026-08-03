# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Gate: the go-live operator checklist must list EVERY automation switch, in the right group.

The three checklist files (`docs/go-live/3-seed/db-seeds/go-live-config-checklist/`) are the operator's
"what do I turn on" map. They drifted once already: the versioning + wait/resume work added
`Workflow::Engine::sends`, `Task::Automation::resume` and `Partner::Idempotency::cleanup` to the
registry, and the checklist was never updated. This gate makes that class of drift a red build.

The registry (`AUTOMATIONS`) is the source of truth. The relation is exact set equality on the toggle
rows: every switch key has a checklist row (nothing an operator could forget to flip), and no checklist
row points at a switch that no longer exists (nothing removed lingering). Grouping is asserted too, so a
key can't be parked under the wrong heading. A planted-bad proves the gate bites.

No DB writes and no site context — reads `AUTOMATIONS` and the checklist JSON in memory only.
"""
import json
import unittest
from pathlib import Path

import frappe

from tatva_connect.automation.registry import AUTOMATIONS

_MODEL_ANCHOR = "const MODEL = "

_CHECKLIST = Path(frappe.get_app_path("tatva_connect")).parent / (
	"docs/go-live/3-seed/db-seeds/go-live-config-checklist"
)


def _expected_group(key):
	"""Which checklist group a switch key belongs under — the same prefix rule the checklist uses."""
	if key.startswith("Task::Automation::"):
		return "automation"
	head = key.split("::", 1)[0]
	return {
		"Workflow": "automation",  # the Flow engine is the automation now (Workflow::Engine::run/sweep)
		"WhatsApp": "whatsapp",
		"Telephony": "telephony",
		"Storage": "storage",
		"Location": "location",
		"Intake": "intake",
		"Partner": "partner",
		"Notify": "notify",
		"Observability": "observability",
	}.get(head, "leadtask")  # Lead:: / Task::Assignment / Task::CRM Task / Note:: / Activity:: -> leadtask


def _registry_keys():
	return {a.key for a in AUTOMATIONS}


def _state_toggles():
	"""{key: group} for every toggle row in checklist-state.json (the machine mirror)."""
	items = json.loads((_CHECKLIST / "checklist-state.json").read_text())["items"]
	return {v["label"]: v["group"] for v in items.values() if v["type"] == "toggle"}


def _html_model():
	"""The model embedded in the interactive checklist, as the browser receives it."""
	html = (_CHECKLIST / "checklist.html").read_text()
	i = html.index(_MODEL_ANCHOR) + len(_MODEL_ANCHOR)
	model, _ = json.JSONDecoder().raw_decode(html, i)
	return model


def _html_toggle_keys():
	return {it["label"] for it in _html_model()["items"].values() if it["type"] == "toggle"}


@unittest.skipUnless(_CHECKLIST.exists(), "checklist ships with the repo, not the deployed image")
class TestGoLiveChecklistParity(unittest.TestCase):
	# (a) the gate: every registry switch is a checklist row, and every checklist toggle is a real
	# switch. Exact equality - a new switch with no checklist row fails here.
	def test_registry_and_checklist_toggles_match_exactly(self):
		self.assertEqual(_registry_keys(), set(_state_toggles()))

	# (b) each switch sits under its correct group heading.
	def test_every_switch_in_correct_group(self):
		misfiled = {k: (g, _expected_group(k)) for k, g in _state_toggles().items() if g != _expected_group(k)}
		self.assertEqual(misfiled, {}, f"switches under the wrong group (got, expected): {misfiled}")

	# (c) the interactive html model and the json mirror carry the same toggle set (no half-applied edit).
	def test_html_model_matches_state_json(self):
		self.assertEqual(_html_toggle_keys(), set(_state_toggles()))

	# (d) the committed artefacts still ARE the registry: rebuild the model and compare. A switch whose
	# purpose, trigger or dependency changed in code but not here fails on content, not merely on the key set.
	def test_the_artefacts_are_in_step_with_the_registry(self):
		from importlib import util

		spec = util.spec_from_file_location("golive_build", _CHECKLIST / "build.py")
		build = util.module_from_spec(spec)
		spec.loader.exec_module(build)
		fresh = build.model()
		on_disk = json.loads((_CHECKLIST / "checklist-state.json").read_text())
		self.assertEqual(
			{k: dict(v, done=False) for k, v in fresh["items"].items()},
			{k: dict(v, done=False) for k, v in on_disk["items"].items()},
			"checklist-state.json is out of step with AUTOMATIONS - run build.py",
		)
		self.assertEqual(fresh["items"], _html_model()["items"], "checklist.html is out of step - run build.py")

	# (e) planted-bad (recall guard, S.6): drop a real switch from the checklist view and the gate MUST
	# fail - proves equality bites, not just that today's files happen to line up.
	def test_gate_bites_on_a_missing_switch(self):
		crippled = set(_state_toggles())
		crippled.discard("Workflow::Engine::run")
		self.assertNotEqual(_registry_keys(), crippled)


if __name__ == "__main__":
	unittest.main()
