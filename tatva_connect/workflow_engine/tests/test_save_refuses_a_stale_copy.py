# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A DRAFT SAVE FROM A STALE COPY IS REFUSED — two editors cannot overwrite each other.

Each node save re-saves the workflow, so `save_draft` compares the `modified` the canvas loaded before it
writes anything, as Frappe's print-format builder does. The saved answer carries the new `modified`, so
the same editor's next save goes through.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.workflow_engine.tests.test_save_refuses_a_stale_copy
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.workflow_engine.tests import fixtures
from tatva_connect.workflow_engine.tests.test_save_answers_with_the_document import _wire
from tatva_connect.workflows import api as workflows_api

_WF = "ZZ Save Refuses A Stale Copy"


class TestSaveRefusesAStaleCopy(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fixtures.purge(_WF)
		cls.addClassCleanup(fixtures.purge, _WF)
		nodes = [fixtures.trigger(to="n1"), fixtures.node("n1", "Terminal")]
		cls.workflow = fixtures.make_workflow(_WF, nodes, lifecycle_state="Draft").name
		cls.graph = frappe.as_json(_wire(nodes))

	def test_a_second_editor_saving_the_copy_they_loaded_is_refused(self):
		loaded = workflows_api.get_workflow(self.workflow)["modified"]
		first = workflows_api.save_draft(self.workflow, self.graph, modified=loaded)
		with self.assertRaises(frappe.TimestampMismatchError):
			workflows_api.save_draft(self.workflow, self.graph, modified=loaded)
		workflows_api.save_draft(self.workflow, self.graph, modified=first["modified"])
