# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""`disable_bulk_complete` really refuses a bulk completion, and the refusal names the type.

Phase 11(a) of docs/plans/task-form-layer/2026-07-25-task-slots-to-sections-and-form-layer.md.

The flag has existed on `CRM Task Type` since Phase 1 and NOTHING read it — an operator could tick it and
a rep could still close twenty visits from the list without opening one form. This suite is what makes it
mean something.

WHY THE ENTRY POINT AND NOT `validate`. The list's bulk action is Frappe's own
`bulk_update.submit_cancel_or_update_docs`, and its `_bulk_action` wraps every per-document save in
`except Exception: log_error(); failed.append(name)`. A `frappe.throw` inside `validate` is therefore
SWALLOWED: the rep gets a success toast, the tasks stay open, and only an Error Log records why. And in
principle `validate` cannot express this rule at all — it cannot tell the bulk lane from the rep's own
form, and the flag is about the lane. So the refusal lives in the wrapper
`tasks.tasks.submit_cancel_or_update_docs`, wired through `override_whitelisted_methods`.

The assertions are OUTCOMES, and the ones that matter are the negative-space ones:
  * every selected task is still not-Done — including tasks of a type that does NOT carry the flag, because
    a partial bulk that silently skipped some of the selection is how a rep comes to believe an activity
    was logged when no form was ever filled;
  * a selection of only unflagged types really does complete, or the refusal above proves nothing;
  * bulk-editing a different field on a flagged type is still allowed — the flag is about completion;
  * the message carries the type's clean label and never its composite `::` primary key.

The wiring is asserted too. A wrapper nothing dispatches to is exactly the shape of bug that let the old
workflow guard scan stay green after the key it read was removed.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.tasks import tasks
from tatva_connect.taxonomy import labels
from tatva_connect.tests.activity import task_type_fixture as tt

_BLOCKED = "ZZ Phase11 Bulk Blocked"
_ALLOWED = "ZZ Phase11 Bulk Allowed"

_NATIVE = "frappe.desk.doctype.bulk_update.bulk_update.submit_cancel_or_update_docs"
_OVERRIDE = "tatva_connect.tasks.tasks.submit_cancel_or_update_docs"


class TestBulkCompleteRefusal(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		# An empty schema on purpose: these types are plain to-dos, so no activity-form guard can be the
		# thing that refuses a save and be mistaken for this one.
		cls.blocked = tt.mint_type(_BLOCKED, schema=[])
		cls.allowed = tt.mint_type(_ALLOWED, schema=[])
		# The flag is operator data, so it is set on the minted row rather than restated in the fixture.
		frappe.db.set_value("CRM Task Type", cls.blocked, "disable_bulk_complete", 1)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		tt.teardown()

	def setUp(self):
		self.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "ZZ Phase11 Bulk", "lead_name": "ZZ Phase11 Bulk Lead",
			"mobile_no": "9876500021",
			"custom_vertical": tt.VERTICAL, "custom_group": tt.GROUP,
		}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input
		frappe.db.commit()  # the bulk lane commits per document, so this suite cannot rely on the rollback

	def tearDown(self):
		for name in frappe.get_all("CRM Task", filters={"reference_docname": self.lead.name}, pluck="name"):
			frappe.delete_doc("CRM Task", name, force=True, ignore_permissions=True)
		frappe.delete_doc("CRM Lead", self.lead.name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _tasks(self, task_type, count):
		names = []
		for i in range(count):
			names.append(frappe.get_doc({
				"doctype": "CRM Task", "title": f"ZZ Phase11 bulk probe {i}", "status": "Todo",
				"custom_task_type": task_type,
				"reference_doctype": "CRM Lead", "reference_docname": self.lead.name,
			}).insert(ignore_permissions=True).name)  # authz-ok: tier-c — test fixture, no user input
		frappe.db.commit()
		return names

	def _bulk(self, names, data):
		return tasks.submit_cancel_or_update_docs("CRM Task", names, "update", data)

	def _statuses(self, names):
		return [frappe.db.get_value("CRM Task", n, "status") for n in names]

	# --- the wiring ------------------------------------------------------------------------------------

	def test_the_native_bulk_endpoint_is_overridden_to_this_wrapper(self):
		"""A gate nothing dispatches to is decoration. Frappe resolves the override by string, so nothing
		but this reads the wiring."""
		declared = frappe.get_hooks("override_whitelisted_methods").get(_NATIVE) or []
		if isinstance(declared, str):
			declared = [declared]
		self.assertIn(_OVERRIDE, declared, "the list's bulk action still reaches the ungated native method")

	# --- the refusal -----------------------------------------------------------------------------------

	def test_three_tasks_of_a_flagged_type_are_refused_and_all_three_stay_open(self):
		names = self._tasks(self.blocked, 3)

		with self.assertRaises(frappe.exceptions.ValidationError):
			self._bulk(names, {"status": "Done"})

		self.assertEqual(
			self._statuses(names), ["Todo", "Todo", "Todo"],
			"the bulk completion was refused, so not one of the selected tasks may be Done",
		)

	def test_the_refusal_names_the_type_and_never_its_composite_key(self):
		names = self._tasks(self.blocked, 1)

		with self.assertRaises(frappe.exceptions.ValidationError) as caught:
			self._bulk(names, {"status": "Done"})

		message = str(caught.exception)
		self.assertIn(
			labels.label(self.blocked, labels.TASK_TYPE), message,
			"the rep cannot tell which activity refused; the message must name the type",
		)
		self.assertNotIn("::", message, "the grain-composite primary key leaked into a rep-facing message")

	def test_one_flagged_task_in_the_selection_refuses_the_WHOLE_call(self):
		"""The negative-space assertion. A partial bulk that quietly skipped the flagged rows would leave a
		rep believing every selected activity was logged."""
		allowed = self._tasks(self.allowed, 2)
		blocked = self._tasks(self.blocked, 1)

		with self.assertRaises(frappe.exceptions.ValidationError):
			self._bulk(allowed + blocked, {"status": "Done"})

		self.assertEqual(
			self._statuses(allowed + blocked), ["Todo", "Todo", "Todo"],
			"part of the selection was completed anyway, so the refusal is not atomic",
		)

	# --- and everything else still works ---------------------------------------------------------------

	def test_a_selection_of_unflagged_types_really_does_complete(self):
		"""Without this the refusal above could be a wrapper that blocks everything."""
		names = self._tasks(self.allowed, 2)

		self._bulk(names, {"status": "Done"})

		self.assertEqual(self._statuses(names), ["Done", "Done"], "the unflagged bulk complete must go through")

	def test_bulk_editing_another_field_on_a_flagged_type_is_allowed(self):
		"""The flag reads 'cannot be completed from the list's bulk action' — it is about completion, not
		about the list. A rep may still reprioritise a hundred visits at once."""
		names = self._tasks(self.blocked, 2)

		self._bulk(names, {"priority": "High"})

		self.assertEqual(
			[frappe.db.get_value("CRM Task", n, "priority") for n in names], ["High", "High"],
			"an unrelated bulk edit was refused; the flag is about completion only",
		)
