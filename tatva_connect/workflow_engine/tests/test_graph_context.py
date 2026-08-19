# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""ONE RESOLVED VIEW OF THE GRAPH, SO A PER-TYPE RULE NEVER HAS TO LIVE OUTSIDE THE TYPE TABLE.

There were two validators. `registry.validate_node` saw one node and `{node_id: config}` and drove off
the type table; `graph.py` saw the whole graph and was hand-written. A rule landed in whichever one could
REACH the facts it needed rather than where it belonged, so two per-type switches grew in `graph.py`:

    graph.py:86   if field["type"] == "Target" and value not in reachable:
    graph.py:92   if field["type"] != "Field" or not value:

which is the exact shape W2.1 existed to delete, surviving only because that is where `subject` was
reachable. `node_type == registry.TRIGGER` was written FOUR times in the same file for the same reason.

The context resolves the graph-level facts ONCE — the Trigger, the subject, the workflow's grain, and the
config map — and hands them to every check. `Target`, `Field` and `Link` become table rows, the switches
go, and the next rule needing a graph fact has somewhere to live instead of stranding.

A CHECK MAY NOT INVENT. `crm_workflow_node.py` validates a single node in DRAFT with no graph at all. A
check that cannot answer without the context returns NO problem: a false refusal blocks an author who has
no way forward, which is strictly worse than a rule that waits for publish.
"""
import json
import pathlib
import re
import unittest

from frappe.tests.utils import FrappeTestCase

from tatva_connect.workflow_engine import graph, registry

_SUBJECT = "CRM Lead"
_GRAIN = {"vertical": "Tatvapractice", "group": "India", "program": ""}


def _node(node_id, node_type, config=None, edges=None):
	return {
		"node_id": node_id, "node_type": node_type,
		"config_json": json.dumps(config or {}),
		"edges": [{"from_output": o, "to_node": t} for o, t in (edges or {}).items()],
	}


def _graph(update_config=None):
	"""Trigger at a grain → Update Field → End. `Update Field` is the node that carries both a Target and
	a Field, which is what the two deleted switches existed for."""
	return [
		_node("trigger-1", "Trigger", {"subject_doctype": _SUBJECT, "event": "Created", **_GRAIN},
		      {"next": "set-1"}),
		_node("set-1", "Update Field", update_config or {}, {"next": "end-1"}),
		_node("end-1", "Terminal"),
	]


def _context(nodes=None):
	return registry.graph_context(nodes if nodes is not None else _graph())


class TestTheContextResolvesTheGraphOnce(FrappeTestCase):
	"""THE red: there was no resolved view at all, only `{node_id: config}`."""

	def test_it_finds_the_trigger(self):
		self.assertEqual(_context()["trigger"]["node_id"], "trigger-1")

	def test_it_resolves_the_subject_the_trigger_watches(self):
		self.assertEqual(_context()["subject"], _SUBJECT)

	def test_it_resolves_the_workflows_grain(self):
		"""A RULE grain: a blank axis means ANY and must survive as blank, never as a literal empty
		string compared exactly. That defect once hid 129 fields from 1,894 leads with tests green."""
		self.assertEqual(_context()["grain"]["vertical"], "Tatvapractice")
		self.assertFalse(_context()["grain"].get("program"), "a blank axis must stay blank")

	def test_it_still_carries_the_config_map_every_caller_already_used(self):
		self.assertEqual(_context()["configs"]["trigger-1"]["subject_doctype"], _SUBJECT)

	def test_a_graph_with_no_trigger_resolves_to_nothing_rather_than_raising(self):
		"""An author builds a graph before adding its Trigger. Resolution must not throw at them."""
		found = _context([_node("end-1", "Terminal")])
		self.assertIsNone(found["trigger"])
		self.assertEqual(found["subject"], "")

	def test_it_carries_every_trigger_so_the_duplicate_rule_needs_no_second_walk(self):
		nodes = [*_graph(), _node("trigger-2", "Trigger", {"subject_doctype": _SUBJECT})]
		self.assertEqual(len(_context(nodes)["triggers"]), 2)


class TestTargetAndFieldAreTableRows(FrappeTestCase):
	"""MOVED off `graph._write_target_problems` and onto their rows. The rule is unchanged; where it lives
	is the whole point."""

	def _messages(self, config):
		return " | ".join(p["message"] for p in graph.problems(_graph(config), entry_node="trigger-1"))

	# `CRM Organization` is the stand-in for out-of-scope: a REAL doctype that is neither the lead, the
	# subject nor a declared write target. It used to be `Sales Invoice`, which is not installed here — so
	# the gate raised DoesNotExistError before it could refuse, and the suite failed for the wrong reason.
	def test_a_target_the_run_can_never_reach_is_refused(self):
		self.assertIn("CRM Organization", self._messages({"target_doctype": "CRM Organization", "updates": [{"name": "x", "mode": "Literal", "value": ""}]}))

	def test_the_subject_is_a_reachable_target(self):
		found = self._messages({"target_doctype": _SUBJECT, "updates": [{"name": "status", "mode": "Literal", "value": "New"}]})
		self.assertNotIn("which this workflow never touches", found)

	def test_a_field_automation_may_not_set_is_refused(self):
		found = self._messages({"target_doctype": _SUBJECT, "updates": [{"name": "zz_not_settable", "mode": "Literal", "value": "x"}]})
		self.assertIn("zz_not_settable", found)

	def test_the_checks_live_on_the_rows(self):
		self.assertIsNotNone(registry.FIELD_TYPES["Target"]["check"])
		self.assertIsNotNone(registry.FIELD_TYPES["Field Map"]["check"])


class TestAGrainScopedLinkAsksTheOneMatcher(FrappeTestCase):
	"""B9 — `grain.overlaps`, rule against rule, blank axis = ANY. Never an exact tuple compare."""

	def test_the_check_lives_on_the_link_row(self):
		self.assertIsNotNone(registry.FIELD_TYPES["Link"]["check"])

	def test_it_uses_the_one_matcher_and_not_a_second_comparison(self):
		import inspect

		source = inspect.getsource(registry.FIELD_TYPES["Link"]["check"])
		self.assertIn("overlaps", source, "a grain question answered without the one matcher")

	def test_a_link_whose_target_carries_no_axes_is_never_grain_checked(self):
		"""Most links carry no grain at all — checking them would refuse every valid value. Derived from
		the target's own schema, exactly as `_scope_kind` already derives which controls are narrowed."""
		field = {"name": "webhook_endpoint", "label": "Endpoint", "type": "Link", "link": "Webhook"}
		self.assertEqual(registry.FIELD_TYPES["Link"]["check"]("anything", field, {}, _context()), [])

	def test_a_workflow_with_no_grain_declared_accepts_anything(self):
		"""Blank on a RULE means ANY. Comparing it as an empty string is the 129-fields defect."""
		blank = _context([_node("trigger-1", "Trigger", {"subject_doctype": _SUBJECT})])
		field = {"name": "task_type", "label": "Task Type", "type": "Link", "link": "CRM Task Type"}
		self.assertEqual(registry.FIELD_TYPES["Link"]["check"]("anything", field, {}, blank), [])


class TestAContextlessCallerGetsNoInventedProblems(FrappeTestCase):
	"""`crm_workflow_node.py` validates one node in DRAFT with no graph. Every context-dependent check
	must abstain there rather than guess."""

	def test_no_graph_no_problems_from_the_context_dependent_rows(self):
		found = registry.validate_node(
			"Update Field", {"target_doctype": "CRM Organization", "fieldname": "whatever"}, [],
			mode=registry.PUBLISH, graph_context=None,
		)
		messages = " | ".join(p["message"] for p in found)
		self.assertNotIn("never touches", messages)
		self.assertNotIn("not a field automation", messages)

	def test_the_rules_that_need_nothing_still_fire_without_a_graph(self):
		"""Abstaining must not become "validate nothing"."""
		found = registry.validate_node(
			"Trigger", {"subject_doctype": _SUBJECT, "event": "Exploded"}, [],
			mode=registry.PUBLISH, graph_context=None,
		)
		self.assertIn("Exploded", " | ".join(p["message"] for p in found))


class TestANewTypeGetsAllThreeFree(FrappeTestCase):
	"""THE CHUNK'S CLAIM. W7 adds six node types next; each must validate the day it is declared.

	The write-target example is a `Field Map` rather than a `Field`: W8.1 moved `Update Field` onto rows
	and left `Field` declared by nothing, so the table's own orphan lock removed the row. The claim under
	test is unchanged — a type declared today gets its Target, its write-target and its Link checks free —
	and it is now made with a type that really exists rather than one only this test declared.
	"""

	def _declared(self, extra_fields):
		declared = dict(registry.NODE_TYPES["Update Field"])
		declared["config"] = [*declared["config"], *extra_fields]
		return declared

	def test_a_type_declared_today_validates_its_target_field_and_link(self):
		from unittest.mock import patch

		fields = [
			{"name": "zz_target", "label": "ZZ Target", "type": "Target"},
			{"name": "zz_fields", "label": "ZZ Fields", "type": "Field Map", "doctype_from": "zz_target"},
			{"name": "zz_link", "label": "ZZ Link", "type": "Link", "link": "CRM Task Type"},
		]
		config = {"zz_target": "CRM Organization", "zz_link": "nope",
		          "zz_fields": [{"name": "nope", "mode": "Literal", "value": "x"}]}
		with patch.dict(registry.NODE_TYPES, {"Update Field": self._declared(fields)}):
			found = registry.validate_node(
				"Update Field", config, [], mode=registry.PUBLISH, graph_context=_context(),
			)
		named = {p["field"] for p in found}
		self.assertIn("zz_target", named, "a declared Target was not validated")


class TestGraphNoLongerSwitchesOnType(unittest.TestCase):
	"""B12 — forbids the SHAPE, and the scan surface is asserted BEFORE the pattern is trusted. One lock
	on this surface already read as coverage it did not have: it matched `field["type"] ==` and missed
	`field.get("type") ==`. Both forms are covered here."""

	_GRAPH = pathlib.Path(graph.__file__)
	_SWITCH = re.compile(r"""\[.type.\]\s*==|\[.type.\]\s*!=|\.get\(.type.\)\s*[=!]=""")

	def test_the_scan_surface_is_the_file_that_held_the_switches(self):
		source = self._GRAPH.read_text()
		self.assertIn("def problems", source, "the lock is not pointed at the graph validator")
		self.assertIn("_reference_problems", source)

	def test_no_per_field_type_switch_survives_in_graph(self):
		offenders = [
			f"graph.py:{n}" for n, line in enumerate(self._GRAPH.read_text().splitlines(), 1)
			if self._SWITCH.search(line) and not line.strip().startswith("#")
		]
		self.assertEqual(offenders, [], f"a per-type switch is still in graph.py at: {offenders}")

	def test_the_trigger_is_resolved_once(self):
		"""Four lookups became one. Counted rather than eyeballed, so a fifth cannot creep back."""
		found = [
			n for n, line in enumerate(self._GRAPH.read_text().splitlines(), 1)
			if "registry.TRIGGER" in line and not line.strip().startswith("#")
		]
		self.assertEqual(found, [], f"graph.py still looks the Trigger up itself at: {found}")
