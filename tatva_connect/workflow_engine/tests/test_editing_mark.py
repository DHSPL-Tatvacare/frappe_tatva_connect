# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""WHO IS EDITING IS A MARK ONLY A WRITER SETS, ANNOUNCED ONLY TO THE RECORD'S ROOM.

`start_editing` marks the caller and `get_workflow` carries the mark, so a viewer who opens the canvas later
still sees it. A user who may not write can neither set nor clear it, and the announcement names the record
so it reaches only sockets Frappe admitted to that doc room.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.workflow_engine.tests.test_editing_mark
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.authoring import editing
from tatva_connect.workflow_engine.tests import fixtures
from tatva_connect.workflows import api as workflows_api

_WF = "ZZ Editing Mark"
_READER = "editing.mark.reader@example.com"


class TestEditingMark(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fixtures.purge(_WF)
		cls.addClassCleanup(fixtures.purge, _WF)
		cls.workflow = fixtures.make_workflow(_WF, [fixtures.trigger(to="n1"), fixtures.node("n1", "Terminal")]).name
		fixtures.make_user(_READER)

	def setUp(self):
		self.addCleanup(frappe.set_user, "Administrator")
		self.addCleanup(frappe.cache.hdel, editing._KEY, editing._field("CRM Workflow", self.workflow))

	def test_a_writer_marks_the_record_and_its_room_hears_it(self):
		with patch.object(frappe, "publish_realtime") as published:
			workflows_api.start_editing(self.workflow)
		self.assertEqual(workflows_api.get_workflow(self.workflow)["editor"], "Administrator")
		published.assert_called_once_with(
			editing.EVENT,
			{"doctype": "CRM Workflow", "name": self.workflow, "user": "Administrator"},
			doctype="CRM Workflow",
			docname=self.workflow,
		)

	def test_a_user_who_may_not_write_neither_sets_nor_clears_it(self):
		workflows_api.start_editing(self.workflow)
		frappe.set_user(_READER)
		with self.assertRaises(frappe.PermissionError):
			workflows_api.start_editing(self.workflow)
		with self.assertRaises(frappe.PermissionError):
			workflows_api.stop_editing(self.workflow)
		frappe.set_user("Administrator")
		self.assertEqual(editing.editor("CRM Workflow", self.workflow), "Administrator")
