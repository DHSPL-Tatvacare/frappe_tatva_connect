# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""No composite `::` primary key ever reaches a human. One invariant, machine-checked.

Six doctypes are named by a `format:` autoname built from the grain, so their PRIMARY KEY is data:
`CRM Task Type` is `{vertical}::{group}::{program}::{type_name}`. A Link field pointing at one stores
that composite string, and printing it hands the rep `GoodFlip Care::Anaya::::Identify PSP Category`.

Resolving it is not the hard part — `taxonomy.labels` does that in one line. REMEMBERING to is. The
label was resolved in four places and forgotten in eight, because every hand-rolled payload has to opt
in and nothing ever told us when one didn't. So this module does not test eight fixes. It tests the
invariant, once, over every read path a human actually looks at:

    a payload may carry a composite PK ONLY when its clean label rides beside it.

Both halves are load-bearing. A PK merely REPLACED by its label breaks the client, which filters,
saves and looks up config on the key. A PK with no label beside it is what the rep is staring at.

`composite_doctypes` derives the doctype set at RUNTIME from each DocType's `autoname`, so a composite
doctype invented next year is covered without anyone editing this file. Adding a read endpoint costs
one line here. That is the whole anti-drift mechanism — there is nothing else to remember.
"""
import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

VERTICAL, GROUP = "GoodFlip Care", "Anaya"


def composite_doctypes():
	"""Every doctype whose PK is data — a `format:` autoname joined by `::`. Read from meta, not a list."""
	return {
		d.name
		for d in frappe.get_all("DocType", fields=["name", "autoname"])
		if "::" in (d.autoname or "")
	}


def _has_label_beside_it(row, key):
	"""Does the row carry the clean label for `key`? Two shapes, both legal and both already in use:
	`{k: PK, k_label: label}` for a record, `{name: PK, label: label}` for an option list."""
	sibling = "label" if key == "name" else f"{key}_label"
	value = row.get(sibling)
	return bool(value) and "::" not in str(value)


def pk_leaks(node, path="$"):
	"""Every place a composite PK reaches the client naked. A string carrying `::` is a leak unless its
	key also carries the label — then the PK is an identity key, which MUST stay a PK."""
	leaks = []
	if isinstance(node, dict):
		for key, value in node.items():
			if isinstance(value, str) and "::" in value and not _has_label_beside_it(node, key):
				leaks.append(f"{path}.{key} = {value!r}")
			else:
				leaks.extend(pk_leaks(value, f"{path}.{key}"))
	elif isinstance(node, (list, tuple)):
		for i, item in enumerate(node):
			leaks.extend(pk_leaks(item, f"{path}[{i}]"))
	return leaks


class TestTheGuardItself(IntegrationTestCase):
	"""A guard that quietly stops guarding is worse than none. Pin what it is looking for."""

	def test_the_composite_doctypes_are_discovered_from_meta(self):
		found = composite_doctypes()
		self.assertIn("CRM Task Type", found, "the guard no longer sees the doctype that started this")
		self.assertIn("CRM Lead Stage", found)

	def test_a_naked_pk_is_a_leak_and_a_labelled_one_is_not(self):
		self.assertTrue(pk_leaks({"activity_type": "A::B::C::Punch"}))
		self.assertFalse(pk_leaks({"activity_type": "A::B::C::Punch", "activity_type_label": "Punch"}))
		self.assertFalse(pk_leaks({"name": "A::B::C::Punch", "label": "Punch"}))
		self.assertTrue(pk_leaks({"detail": "Create Task A::B::C::Punch due tomorrow"}))
		self.assertTrue(pk_leaks([{"rows": [{"type": "A::B::C::Punch"}]}]), "must walk nested lists")


class TestNoCompositePKReachesTheUI(IntegrationTestCase):
	"""Every read path a rep or an operator actually looks at. IntegrationTestCase rolls the fixture
	back per test — no savepoint bookkeeping, and nothing leaks even when a test fails."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.task_type = frappe.db.get_value(
			"CRM Task Type",
			{"name": ["like", f"{VERTICAL}::{GROUP}::%"], "program": ["in", ["", None]]},
			"name",
		)
		if not cls.task_type or "::" not in cls.task_type:
			raise cls.skipTest(cls, f"no composite, program-agnostic task type on {VERTICAL}::{GROUP}")
		cls.type_name = frappe.db.get_value("CRM Task Type", cls.task_type, "type_name")

	def setUp(self):
		from tatva_connect.activity.api import save_activity

		self.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Label Probe",
			"mobile_no": f"+9198124{frappe.generate_hash(length=5)[:5]}",
			"custom_vertical": VERTICAL, "custom_group": GROUP,
		}).insert(ignore_permissions=True)
		# Punched through the one real writer — a task cannot be marked Done without being logged.
		self.task = save_activity(self.lead.name, self.task_type, {}, task=None)
		# A second, still-OPEN activity: the completed one above is invisible to the open-task mapping.
		frappe.get_doc({
			"doctype": "CRM Task", "title": "Label Probe Open",
			"reference_doctype": "CRM Lead", "reference_docname": self.lead.name,
			"custom_task_type": self.task_type, "status": "Todo",
		}).insert(ignore_permissions=True)

	def _assert_clean(self, payload, screen):
		"""A guard that passes on an empty payload guards nothing, so prove the endpoint actually
		returned the seeded activity BEFORE trusting that it returned it clean."""
		blob = json.dumps(payload, default=str)
		self.assertTrue(
			self.task_type in blob or self.type_name in blob,
			f"{screen} said nothing about the seeded activity — this assertion would pass vacuously",
		)
		leaks = pk_leaks(payload)
		self.assertEqual(leaks, [], f"{screen} shows a raw composite PK:\n  " + "\n  ".join(leaks))

	def test_the_lead_timeline_projection(self):
		"""Feeds the SPA Activity tab AND the Desk timeline — the screen that started this."""
		from tatva_connect.activity.api import lead_timeline

		self._assert_clean(lead_timeline(self.lead.name), "the lead timeline")

	def test_the_activity_tab(self):
		"""What the rep sees on the lead: `<rep> completed <activity type>`."""
		from tatva_connect.api.activities import get_activities

		self._assert_clean(get_activities(self.lead.name), "the Activity tab")

	def test_the_desk_activity_and_location_panel(self):
		"""The Desk 'Activity & Location' section prints each row's type verbatim."""
		from tatva_connect.location.api import lead_location_view

		self._assert_clean(lead_location_view(self.lead.name), "the Desk Activity & Location panel")

	def test_the_open_activity_tasks_mapping(self):
		from tatva_connect.activity.api import open_activity_tasks

		self._assert_clean(open_activity_tasks(self.lead.name), "the open-activity mapping")

	def test_the_tasks_board(self):
		"""Already correct — pinned so the one place that got it right cannot regress."""
		from tatva_connect.activity.api import lead_task_board

		self._assert_clean(lead_task_board(self.lead.name), "the Tasks board")

	def test_the_activity_type_picker(self):
		"""Already correct — pinned for the same reason."""
		from tatva_connect.activity.api import list_types_for_lead

		self._assert_clean(list_types_for_lead(self.lead.name), "the activity-type picker")

	def test_the_smart_view_tabs(self):
		from tatva_connect.smartview.api import get_smart_views

		# owner_user, or get_smart_views' or_filters never return it and this passes on an empty list.
		frappe.get_doc({
			"doctype": "CRM Smart View", "label": "Label Probe View",
			"base_object": "Activity", "activity_type": self.task_type,
			"owner_user": frappe.session.user,
		}).insert(ignore_permissions=True)
		self._assert_clean(get_smart_views(), "the Smart View tabs")

	def test_the_automation_run_log(self):
		"""The per-action audit line an operator reads after a rule fires."""
		from tatva_connect.automation import actions

		action = frappe._dict(action_type="Create Task", task_type=self.task_type)
		self._assert_clean({"detail": actions._action_label(action)}, "the automation run log")

	def test_the_rule_simulation_preview(self):
		"""The dry-run line an operator reads BEFORE arming a rule."""
		from tatva_connect.automation import simulate

		action = frappe._dict(action_type="Create Task", task_type=self.task_type,
		                      due_mode=None, due_from=None, due_expression=None)
		with patch("tatva_connect.automation.actions._due_at", return_value=None):
			preview = simulate._would_create_task(action, {}, self.lead.name, (VERTICAL, GROUP, ""))
		self._assert_clean({"detail": preview}, "the rule simulation preview")
