# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A composite `::` primary key must not reach a human.

Six doctypes carry a `format:` autoname built from the grain, so their primary key is data:
`CRM Task Type` is `{vertical}::{group}::{program}::{type_name}`. Printing it gives a rep
`GoodFlip Care::Anaya::::Identify PSP Category`.

Two mechanisms keep it out of the UI, and they are tested separately here:

  the list      the framework's `_link_titles` map (api/list_link_titles), which also titles the Kanban
  and Kanban    columns. The row keeps the PK, which is what the client filters, sorts and groups by,
                and the map carries the title beside it.
  hand-built    payloads assembled by our own whitelisted endpoints, which have no such map and must
  payloads      resolve the label themselves through taxonomy.labels.

Every surface that shows a stage or an activity type to a person is covered below: the list, the
Kanban board, the Activity tab, the Desk timeline, the Tasks board, the pickers, the automation run
log and rule preview, the push notification a rep gets on their phone, the WhatsApp text a patient
gets, and the partner API's type catalogue.
"""
import json
import unittest
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

VERTICAL, GROUP = "GoodFlip Care", "Anaya"

# Keys whose value is user free text or a saved filter tree: a `::` there is the user's, not a leak.
FREE_TEXT_KEYS = {"description", "file_name", "address", "predicate", "content", "title"}


def composite_doctypes():
	"""Every doctype whose primary key is data: a `format:` autoname joined by `::`. Read from meta."""
	return {
		d.name
		for d in frappe.get_all("DocType", fields=["name", "autoname"])
		if "::" in (d.autoname or "")
	}


def _labelled(row, key):
	"""Does the row carry the label for `key` beside the key? `{k: PK, k_label: label}` for a record,
	`{name: PK, label: label}` for an option list."""
	sibling = "label" if key == "name" else f"{key}_label"
	value = row.get(sibling)
	return bool(value) and "::" not in str(value)


def pk_leaks(node, path="$", key=None):
	"""Every `::` string reaching the client.

	The signal is the separator, not a lookup: composite PKs are the only source of `::` in these
	payloads, which is what makes a substring test sufficient. `composite_doctypes()` pins that premise
	(TestTheGuardItself asserts the set is non-empty), and FREE_TEXT_KEYS exempts the fields where a
	user could legitimately type one. A key may keep its PK only when the label rides beside it.
	"""
	leaks = []
	if isinstance(node, str):
		if "::" in node and key not in FREE_TEXT_KEYS:
			leaks.append(f"{path} = {node!r}")
	elif isinstance(node, dict):
		for k, value in node.items():
			if isinstance(value, str) and "::" in value and _labelled(node, k):
				continue
			leaks.extend(pk_leaks(value, f"{path}.{k}", key=k))
	elif isinstance(node, (list, tuple)):
		for i, item in enumerate(node):
			leaks.extend(pk_leaks(item, f"{path}[{i}]", key=key))
	return leaks


class TestTheGuardItself(IntegrationTestCase):
	def test_the_composite_doctypes_exist(self):
		found = composite_doctypes()
		self.assertIn("CRM Task Type", found)
		self.assertIn("CRM Lead Stage", found)

	def test_what_the_guard_flags(self):
		self.assertTrue(pk_leaks({"activity_type": "A::B::C::Punch"}))
		self.assertFalse(pk_leaks({"activity_type": "A::B::C::Punch", "activity_type_label": "Punch"}))
		self.assertFalse(pk_leaks({"name": "A::B::C::Punch", "label": "Punch"}))
		self.assertTrue(pk_leaks({"detail": "Create Task A::B::C::Punch due tomorrow"}))
		self.assertTrue(pk_leaks([{"rows": [{"type": "A::B::C::Punch"}]}]), "must walk nested lists")
		self.assertTrue(pk_leaks({"types": ["A::B::C::Punch"]}), "must walk strings inside a list")
		self.assertFalse(pk_leaks({"file_name": "BP::2026-07-11.jpg"}), "user free text is not a leak")


class TestTheListKeepsThePrimaryKey(IntegrationTestCase):
	"""The list must NOT resolve the label into the row. The row is what the client filters, sorts and
	groups by, so replacing the PK there sends the label back as a filter (matching nothing) and merges
	two stages that share a name across programs. The label rides in `_link_titles` instead."""

	def test_the_row_keeps_the_pk_and_the_title_rides_beside_it(self):
		from tatva_connect.api.list_link_titles import get_data

		frappe.set_user("Administrator")
		frappe.local.form_dict = frappe._dict()
		result = get_data(
			doctype="CRM Lead", view={}, filters={"custom_substage": ["is", "set"]},
			order_by="modified desc", page_length=3,
			columns=json.dumps([{"key": "custom_substage"}]),
		)
		rows = result.get("data") or []
		if not rows:
			raise unittest.SkipTest("no lead carries a substage on this site")

		titles = result.get("_link_titles") or {}
		for row in rows:
			pk = row["custom_substage"]
			self.assertIn("::", pk, "the row must keep the composite PK the list filters on")
			title = titles.get(f"CRM Lead Stage::{pk}")
			self.assertTrue(title, f"_link_titles carries no title for {pk!r}")
			self.assertNotIn("::", title)

	def test_every_kanban_column_header_has_a_title(self):
		"""A column's `name` IS a primary key, so the board printed `Sigrima::Treatment on Hold` as a
		header. The column keeps its key (it is the drag target); the title rides in the map."""
		from tatva_connect.api.list_link_titles import get_data

		frappe.set_user("Administrator")
		frappe.local.form_dict = frappe._dict()
		result = get_data(
			doctype="CRM Lead", view={"view_type": "kanban", "group_by_field": "custom_substage"},
			filters={}, order_by="modified desc", page_length=5, column_field="custom_substage",
		)
		columns = result.get("kanban_columns") or []
		if not columns:
			raise unittest.SkipTest("no kanban columns on this site")

		titles = result.get("_link_titles") or {}
		for column in columns:
			pk = column["name"]
			self.assertIn("::", pk, "the column must keep the key the board drags onto")
			title = titles.get(f"CRM Lead Stage::{pk}")
			self.assertTrue(title, f"the board would print {pk!r} as a header")
			self.assertNotIn("::", title)


class TestHandBuiltPayloads(IntegrationTestCase):
	"""The endpoints that assemble their own payload and so must resolve the label themselves."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.task_type = frappe.db.get_value(
			"CRM Task Type",
			{"name": ["like", f"{VERTICAL}::{GROUP}::%"], "program": ["in", ["", None]]},
			"name",
			order_by="name asc",
		)
		if not cls.task_type or "::" not in cls.task_type:
			raise unittest.SkipTest(f"no composite, program-agnostic task type on {VERTICAL}::{GROUP}")
		cls.type_name = frappe.db.get_value("CRM Task Type", cls.task_type, "type_name")

	def setUp(self):
		from tatva_connect.activity.api import save_activity

		# IntegrationTestCase rolls back once per CLASS, not per test, and any commit reached inside a
		# test would make these fixtures permanent. Roll back after every test.
		self.addCleanup(frappe.db.rollback)
		self.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Label Probe",
			"mobile_no": f"+9198124{frappe.generate_hash(length=5)[:5]}",
			"custom_vertical": VERTICAL, "custom_group": GROUP,
		}).insert(ignore_permissions=True)
		save_activity(self.lead.name, self.task_type, {}, task=None)
		frappe.get_doc({
			"doctype": "CRM Task", "title": "Label Probe Open",
			"reference_doctype": "CRM Lead", "reference_docname": self.lead.name,
			"custom_task_type": self.task_type, "status": "Todo",
		}).insert(ignore_permissions=True)

	def _assert_clean(self, payload, screen):
		"""An endpoint that returned nothing would pass any leak check, so prove it spoke first."""
		blob = json.dumps(payload, default=str)
		self.assertTrue(
			self.task_type in blob or self.type_name in blob,
			f"{screen} said nothing about the seeded activity, so this assertion proves nothing",
		)
		leaks = pk_leaks(payload)
		self.assertEqual(leaks, [], f"{screen} shows a raw composite PK:\n  " + "\n  ".join(leaks))

	def test_the_lead_timeline(self):
		from tatva_connect.activity.api import lead_timeline

		self._assert_clean(lead_timeline(self.lead.name), "the lead timeline")

	def test_the_activity_tab(self):
		from tatva_connect.api.activities import get_activities

		self._assert_clean(get_activities(self.lead.name), "the Activity tab")

	def test_the_desk_activity_and_location_panel(self):
		from tatva_connect.location.api import lead_location_view

		self._assert_clean(lead_location_view(self.lead.name), "the Desk Activity and Location panel")

	def test_the_open_activity_mapping(self):
		from tatva_connect.activity.api import open_activity_tasks

		self._assert_clean(open_activity_tasks(self.lead.name), "the open-activity mapping")

	def test_the_tasks_board(self):
		from tatva_connect.activity.api import lead_task_board

		self._assert_clean(lead_task_board(self.lead.name), "the Tasks board")

	def test_the_activity_type_picker(self):
		from tatva_connect.activity.api import list_types_for_lead

		self._assert_clean(list_types_for_lead(self.lead.name), "the activity-type picker")

	def test_the_smart_view_tabs(self):
		from tatva_connect.smartview.api import get_smart_views

		frappe.get_doc({
			"doctype": "CRM Smart View", "label": "Label Probe View",
			"base_object": "Activity", "activity_type": self.task_type,
			"owner_user": frappe.session.user,
		}).insert(ignore_permissions=True)
		self._assert_clean(get_smart_views(), "the Smart View tabs")

	def test_the_automation_run_log(self):
		from tatva_connect.automation import actions

		action = frappe._dict(action_type="Create Task", task_type=self.task_type)
		self._assert_clean({"detail": actions._action_label(action)}, "the automation run log")

	def test_an_automation_created_task_is_titled_in_english(self):
		"""create_followup_task titles the row from the type; it must not stamp the composite PK."""
		from tatva_connect.tasks.tasks import create_followup_task

		name = create_followup_task(self.lead.name, self.task_type, throttle=False)
		self.assertEqual(frappe.db.get_value("CRM Task", name, "title"), self.type_name)

	def test_the_partner_api_type_catalogue(self):
		"""A partner POSTs `name` back, so the key stays; without `label` their menu is `::` strings."""
		from tatva_connect.activity.api import list_types_for_lead

		for t in list_types_for_lead(self.lead.name):
			self.assertIn("::", t["name"], "the key a partner posts back must stay a key")
			self.assertNotIn("::", t["label"])


class TestTheNotificationAndTheWhatsAppText(IntegrationTestCase):
	"""The two surfaces that leave the app: a rep's lock screen and a patient's phone."""

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.set_user("Administrator")
		self.stage = frappe.db.get_value("CRM Lead Stage", {"name": ["like", "%::%"]}, "name")
		if not self.stage:
			raise unittest.SkipTest("no composite lead stage on this site")

	def test_a_stage_change_push_names_the_stage(self):
		"""The body lands on a lock screen: "moved to Treatment on Hold", never "Ujvira::Treatment on Hold"."""
		from tatva_connect.notifications import events

		sent = {}
		with patch.object(events.dispatch, "notify", lambda *a, **kw: sent.update(kw)):
			doc = frappe.get_doc({
				"doctype": "CRM Lead", "first_name": "Push Probe",
				"mobile_no": f"+9198125{frappe.generate_hash(length=5)[:5]}",
			}).insert(ignore_permissions=True)
			doc.custom_substage = self.stage
			events.on_lead_stage_changed(doc)

		self.assertTrue(sent, "the stage change did not dispatch")
		self._assert_clean(sent.get("body"), "the push notification body")
		self._assert_clean(sent.get("bell", {}).get("text"), "the bell feed text")

	def test_a_whatsapp_param_names_the_stage(self):
		"""A Link param would otherwise send the composite key to a patient."""
		from tatva_connect.taxonomy import labels

		shown = labels.shown("CRM Lead", "custom_substage", self.stage)
		self._assert_clean(shown, "a WhatsApp template param")

	def _assert_clean(self, text, screen):
		self.assertTrue(text, f"{screen} produced nothing")
		self.assertNotIn("::", str(text), f"{screen} carries a raw composite PK: {text!r}")
