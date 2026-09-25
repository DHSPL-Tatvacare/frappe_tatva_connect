# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""DUPLICATE COPIES THE DEFINITION AND NOTHING THAT RAN.

The copy is a Draft carrying the source's Trigger, nodes, edges, layout and Start node — and no Version,
journey or cohort state, so it cannot fire until its own author publishes and activates it. The source is
left exactly as it was.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.workflow_engine.tests.test_duplicate_is_a_pure_draft_copy
"""
import json
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.workflow_engine.tests import fixtures
from tatva_connect.workflows import api as workflows_api

_SRC = "ZZ Duplicate Source"
_COPY = "ZZ Duplicate Source (copy)"
_HEADER = ("entry_node", "canvas_json", "trigger_doctype", "trigger_event", "trigger_mode",
           "trigger_vertical", "trigger_group", "trigger_program")


def _graph(workflow):
	"""The authored graph, keyed by node id — what a copy must reproduce, without the rows' own names."""
	return {
		row["node_id"]: (row["node_type"], json.loads(row["config_json"] or "{}"),
		                 sorted((e["from_output"], e["to_node"]) for e in row["edges"]))
		for row in workflows_api.get_workflow(workflow)["nodes"]
	}


class TestDuplicateIsAPureDraftCopy(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fixtures.purge(_SRC, _COPY)
		cls.addClassCleanup(fixtures.purge, _SRC, _COPY)
		fixtures.make_workflow(
			_SRC, [fixtures.trigger(to="n1"), fixtures.node("n1", "Terminal")],
			lifecycle_state="Active", canvas={"start": {"x": 10, "y": 20}},
		)
		cls.source_before = frappe.db.get_value("CRM Workflow", _SRC, ["lifecycle_state", "modified"], as_dict=True)
		cls.versions_before = frappe.db.count("CRM Workflow Version", {"workflow": _SRC})
		cls.copy = workflows_api.duplicate(_SRC, _COPY)

	def test_the_copy_is_a_draft_with_the_same_definition(self):
		self.assertEqual(self.copy["lifecycle_state"], "Draft")
		self.assertEqual(_graph(_COPY), _graph(_SRC))
		source = frappe.db.get_value("CRM Workflow", _SRC, _HEADER, as_dict=True)
		self.assertEqual({k: self.copy[k] for k in _HEADER}, dict(source))

	def test_nothing_that_ran_comes_with_it_and_the_source_is_untouched(self):
		self.assertEqual(frappe.db.count("CRM Workflow Version", {"workflow": _COPY}), 0)
		self.assertEqual(frappe.db.count("CRM Workflow Journey", {"workflow": _COPY}), 0)
		self.assertFalse(self.copy.get("cohort_state"))
		self.assertEqual(
			frappe.db.get_value("CRM Workflow", _SRC, ["lifecycle_state", "modified"], as_dict=True), self.source_before
		)
		self.assertEqual(frappe.db.count("CRM Workflow Version", {"workflow": _SRC}), self.versions_before)


if __name__ == "__main__":
	unittest.main()
