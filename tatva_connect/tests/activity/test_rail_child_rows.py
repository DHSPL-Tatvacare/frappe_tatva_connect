# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A save that changes a lead's child rows is a rail line, exactly like a save that changes one of its fields.

The rail read only the `changed` half of frappe's diff, so a returning lead's new screening answers — and every
lab, drug or plan row — drew nothing, and a save touching only rows was not even indexed. Every case below
saves through the document API and reads the Version frappe itself wrote.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.activity.test_rail_child_rows
"""
import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.activity import lead_events, timeline
from tatva_connect.api import activities, partner
from tatva_connect.partner_api.doctype.crm_lead_section import crm_lead_section

SCREENING = "custom_screening_answers"
QUESTION = "zz_rail_do_you_have_high_bp?"


class TestChildRowsReachTheRail(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.lead = frappe.get_doc(
			{"doctype": "CRM Lead", "first_name": "ZZ Rail Rows", "mobile_no": "+919000000071"}
		).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture, runs as Administrator

	def tearDown(self):
		frappe.db.rollback()

	def _save(self, mutate):
		lead = frappe.get_doc("CRM Lead", self.lead.name)
		mutate(lead)
		# frappe skips the Version in a test unless asked (document.py:556) — and the Version is the subject here.
		lead.save(ignore_permissions=True, ignore_version=False)  # authz-ok: tier-a — test fixture, runs as Administrator
		return frappe.get_all(
			"Version", filters={"ref_doctype": "CRM Lead", "docname": self.lead.name},
			fields=["name", "owner", "creation", "data", "docname", "ref_doctype"],
			order_by="creation desc", limit=1,
		)[0]

	def _answer(self, value):
		return lambda lead: partner._apply_children(
			lead, {SCREENING: [{"question": QUESTION, "label": "High BP?", "value": value}]}
		)

	def _lines(self, version):
		return activities._version_row(version, "CRM Lead")["changes"]

	def test_a_new_screening_answer_is_a_line_and_is_indexed(self):
		version = self._save(self._answer("No"))

		self.assertEqual(self._lines(version), [{"label": f"{self._title(SCREENING)} · High BP?", "from": "", "to": "No"}])
		self.assertIsNotNone(timeline.event_row(frappe._dict(version, doctype="Version")))

	def test_a_changed_answer_reads_old_to_new(self):
		self._save(self._answer("No"))
		version = self._save(self._answer("Yes"))

		self.assertEqual(self._lines(version), [{"label": f"{self._title(SCREENING)} · High BP?", "from": "No", "to": "Yes"}])

	def test_an_edited_row_reads_its_column_old_to_new(self):
		self._save(lambda lead: lead.append("custom_lab_profile", {"hba1c": 7.1, "report_date": "2026-09-01"}))
		version = self._save(lambda lead: setattr(lead.custom_lab_profile[0], "hba1c", 6.8))

		[line] = self._lines(version)
		label = frappe.get_meta("CRM Lab Profile").get_field("hba1c").label
		self.assertEqual(line["label"], f"{self._title('custom_lab_profile')} · {label}")
		self.assertEqual((float(line["from"]), float(line["to"])), (7.1, 6.8))

	def test_a_removed_row_reads_as_cleared(self):
		self._save(self._answer("No"))
		version = self._save(lambda lead: lead.set(SCREENING, []))

		self.assertEqual(self._lines(version), [{"label": f"{self._title(SCREENING)} · High BP?", "from": "No", "to": ""}])

	def test_every_section_column_reads_like_the_data_tab(self):
		"""A partner-written counter is a Data tab column like any other, so its change is a line like any other."""
		version = self._save(lambda lead: lead.append("custom_lead_activity_metrics", {"custom_rnr_count": 3}))

		label = frappe.get_meta("CRM Lead Activity Metrics").get_field("custom_rnr_count").label
		self.assertIn({"label": f"{self._title('custom_lead_activity_metrics')} · {label}", "from": "", "to": "3"},
					  self._lines(version))

	def test_a_cleared_lead_field_reads_as_cleared(self):
		self._save(lambda lead: setattr(lead, "website", "https://zz.example"))
		version = self._save(lambda lead: setattr(lead, "website", None))

		self.assertEqual(self._lines(version), [{"label": "Website", "from": "https://zz.example", "to": ""}])

	def _title(self, cf):
		return crm_lead_section.section_for_child(cf).title


class TestAKeptPickIsNoChange(unittest.TestCase):
	"""`multi_value.replace` drops and re-adds a field's whole set, so only a pick that came or went is a line."""

	def _pick(self, value):
		return frappe._dict(field_key="zz:picks", row_key="", value=value)

	def test_a_re_saved_set_draws_nothing(self):
		self.assertEqual(lead_events._selection_entries([self._pick("A"), self._pick("B")], [self._pick("A"), self._pick("B")]), [])

	def test_only_the_new_pick_is_a_line(self):
		[entry] = lead_events._selection_entries([self._pick("A"), self._pick("C")], [self._pick("A")])

		self.assertEqual(entry[3:], (None, "C"))


class TestAClosedTaskIsItsOwnLine(unittest.TestCase):
	"""A task closed later gets its own line — when and by whom — off the row its card already reads."""

	def _task(self, status):
		return {"name": 7, "status": status, "title": "Call back", "modified": "2026-09-22 10:00:00",
				"modified_by": "asha@x.com"}

	def test_done_and_canceled_each_close_it(self):
		for status in activities.TASK_CLOSED_STATES:
			[line] = activities._task_closings([self._task(status)])
			self.assertEqual((line["activity_type"], line["status"], line["owner"], line["creation"]),
							 ("task_closed", status, "asha@x.com", "2026-09-22 10:00:00"))

	def test_an_open_task_has_no_closing(self):
		self.assertEqual(activities._task_closings([self._task("Todo")]), [])


class TestAWorkflowSaveNamesItsWorkflow(unittest.TestCase):
	"""A field a workflow changes reads as that workflow — through frappe's own `updater_reference`, never a new column."""

	def _saved_flags(self, context):
		from tatva_connect.automation import actions

		doc = frappe.new_doc("CRM Lead")
		with patch.object(type(doc), "save"):
			actions._save_target(doc, context=context)
		return doc.flags.get("updater_reference")

	def test_a_workflow_step_stamps_its_run(self):
		from tatva_connect.workflow_engine import refs

		self.assertEqual(self._saved_flags({refs.JOURNEY: "JRN-1"}), {"doctype": "CRM Workflow Journey", "docname": "JRN-1"})

	def test_a_rule_or_a_person_stamps_nothing(self):
		self.assertIsNone(self._saved_flags({}))
		self.assertIsNone(self._saved_flags(None))

	def test_the_run_is_the_steps_alone(self):
		"""Set for the step and popped after it — on success AND on failure — so no journey state ever keeps it."""
		from tatva_connect.workflow_engine import interpreter, refs

		for outcome in ("ok", "raise"):
			state, seen = {}, []

			def handler(params, lead, context, axes, trigger, outcome=outcome):
				seen.append(context.get(refs.JOURNEY))
				if outcome == "raise":
					raise ValueError("boom")

			state_obj = type("S", (dict,), {"writing_as": lambda self, _n: self})(state)
			node = frappe._dict(node_type="Set Field", node_id="set_stage", config="{}")
			with (
				patch.object(interpreter.actions, "handler_of", return_value=handler),
				patch.object(interpreter.actions, "outcomes_of", return_value=()),
				patch.object(interpreter, "_config", return_value={}),
				patch.object(interpreter, "_forget_written"),
				patch("frappe.db.savepoint"), patch("frappe.db.release_savepoint"),
				patch.object(interpreter, "_undo_to"),
			):
				try:
					interpreter._run_verb(node, "LEAD-1", None, state_obj, (), run="JRN-9")
				except ValueError:
					pass
			self.assertEqual(seen, ["JRN-9"])
			self.assertNotIn(refs.JOURNEY, state_obj)

	def test_the_rail_names_the_workflow(self):
		rows = [{"owner": "Administrator", "run": "JRN-1"}, {"owner": "Administrator", "run": None}]
		with patch("tatva_connect.automation.origin.journey_labels", return_value={"JRN-1": "Demo Flow"}):
			activities._name_actors(rows)

		self.assertEqual(rows[0]["automation"], {"label": "Demo Flow", "journey": "JRN-1"})
		self.assertNotIn("automation", rows[1])


class TestAnAssignmentIsALine(unittest.TestCase):
	"""An assignment reaches the rail off frappe's ToDo — who assigned whom, and when it ended — to the microsecond."""

	def _rows(self, todos):
		with patch("frappe.get_all", return_value=[frappe._dict(t) for t in todos]):
			return activities._assignments([("CRM Lead", "LEAD-1")])

	def _todo(self, status):
		from datetime import datetime

		return {"name": "TD-1", "allocated_to": "rep@x.com", "assigned_by": "Guest", "status": status,
				"creation": datetime(2026, 9, 22, 10, 0, 0, 120), "modified": datetime(2026, 9, 22, 11, 0, 0, 450),
				"modified_by": "mgr@x.com"}

	def test_an_open_assignment_is_one_line_by_whoever_assigned_it(self):
		[line] = self._rows([self._todo("Open")])

		self.assertEqual((line["activity_type"], line["owner"], line["assignee"], line["creation"]),
						 ("assigned", "Guest", "rep@x.com", "2026-09-22 10:00:00.000120"))

	def test_an_ended_assignment_adds_who_ended_it_and_when(self):
		for status in ("Closed", "Cancelled"):
			_opened, ended = self._rows([self._todo(status)])
			self.assertEqual((ended["activity_type"], ended["owner"], ended["creation"]),
							 ("unassigned", "mgr@x.com", "2026-09-22 11:00:00.000450"))

	def test_the_assignee_is_named_by_the_one_actor_rule(self):
		rows = [{"owner": "Guest", "assignee": "rep@x.com"}]
		with (
			patch("tatva_connect.activity.actor.partner_users", return_value=frozenset()),
			patch("frappe.get_all", return_value=[{"name": "rep@x.com", "full_name": "Asha Rep"}]),
		):
			activities._name_actors(rows)

		self.assertEqual((rows[0]["owner_name"], rows[0]["assignee_name"]), ("Intake form", "Asha Rep"))
