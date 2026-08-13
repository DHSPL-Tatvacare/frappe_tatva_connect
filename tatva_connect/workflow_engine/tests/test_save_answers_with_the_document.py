# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A DRAFT SAVE ANSWERS WITH THE DOCUMENT IT JUST WROTE — so the canvas has nothing left to re-fetch.

`save_draft` persisted the graph and answered with two columns, so the SPA re-read the whole workflow
straight afterwards: a second round trip for a document the server was already holding, and the re-read is
what re-triggered the graph resolvers. It has re-read the document by then (`api.py doc.reload()`), so the
saved payload is free — and it is `get_workflow`'s payload, never a second shape built beside it.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.workflow_engine.tests.test_save_answers_with_the_document
"""
import json
import unittest

from frappe.tests.utils import FrappeTestCase

from tatva_connect.workflow_engine.tests import fixtures
from tatva_connect.workflows import api as workflows_api

_WF = "ZZ Save Answers With The Document"


def _wire(nodes):
	"""The node rows as the canvas posts them — `config_json` and a list of edges, not the fixture shape."""
	return [
		{
			"node_id": spec["node_id"],
			"node_type": spec["node_type"],
			"config_json": json.dumps(spec.get("config") or {}),
			"edges": [{"from_output": o, "to_node": t} for o, t in (spec.get("edges") or {}).items()],
		}
		for spec in nodes
	]


class TestSaveDraftReturnsTheSavedPayload(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fixtures.purge(_WF)
		cls.addClassCleanup(fixtures.purge, _WF)
		cls.graph = [fixtures.trigger(to="n1"), fixtures.node("n1", "Terminal")]
		cls.workflow = fixtures.make_workflow(_WF, cls.graph, lifecycle_state="Draft").name

	def _save(self):
		return workflows_api.save_draft(self.workflow, json.dumps(_wire(self.graph)))

	def test_it_answers_exactly_what_get_workflow_answers(self):
		self.assertEqual(self._save(), workflows_api.get_workflow(self.workflow))

	def test_the_answer_carries_the_graph_it_just_wrote(self):
		saved = self._save()
		self.assertEqual(
			[row["node_id"] for row in saved["nodes"]], [spec["node_id"] for spec in self.graph]
		)

	def test_it_still_carries_what_it_always_did(self):
		saved = self._save()
		self.assertEqual(saved["name"], self.workflow)
		self.assertEqual(saved["lifecycle_state"], "Draft")


if __name__ == "__main__":
	unittest.main()
