# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""One open ticket per patient — and every OTHER write target left exactly as it was.

`wrote_name` answers "which record has this journey made" within ONE run, and a run is per-save: a
patient who writes five times starts five journeys, each with an empty bucket, so each raised its own
ticket. The reuse lookup is the second half of that answer — is one still OPEN for them.

WHY THE COLLATERAL TESTS MATTER MOST
------------------------------------
The lookup lives in `resolve_target`, which EVERY write verb goes through for every target. So the
class that earns its place here is `TestNothingElseMoved`: a write to the lead and a write to the
triggering record must resolve exactly as before, because those are the two answers every rule shipped
today depends on. The reuse branch is reachable only for a doctype declared in `WRITE_TARGETS`, and
this proves it.

WHAT THE RULE IS
----------------
  * open, same patient          -> reused, no second record
  * finished (a category in `TERMINAL_CATEGORIES`) -> a new one; a closed ticket is not reopened months on
  * a different patient         -> never shared, whatever is open elsewhere
  * no author control at all    -> W8.1 deleted this node's gates; reuse must not re-open them
  * within one run              -> `wrote_name` still wins, so two nodes write ONE record as before

The terminal set and the status field are READ (the master's `category`, the Link that points at it),
and the question is "not finished" — so a site that adds a status, or a whole category, is covered here
without an edit, and errs towards reusing rather than duplicating.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import actions, subjects
from tatva_connect.workflow_engine import refs

_TARGET = "HD Ticket"
_LEAD = "CRM Lead"


def _action(verb, **config):
	return frappe._dict(action_type=verb, **config)


class _ThrottleCase(FrappeTestCase):
	def setUp(self):
		if _TARGET not in subjects.WRITE_TARGETS:
			self.skipTest(f"{_TARGET} is not a declared write target — the feature is dormant")
		self.addCleanup(frappe.db.rollback)
		self.spec = subjects.WRITE_TARGETS[_TARGET]
		self.lead = frappe.db.get_value(_LEAD, {}, "name")
		self.other_lead = frappe.db.get_value(_LEAD, {"name": ["!=", self.lead]}, "name")
		self.ctx = refs.Values(buckets={}).writing_as("n1")

	def _status(self, category):
		"""A real status row in `category` — read back, never a name typed in."""
		name = frappe.db.get_value("HD Ticket Status", {"category": category}, "name")
		if not name:
			self.skipTest(f"this site declares no {category} ticket status")
		return name

	def _raise(self, lead=None, ctx=None, **config):
		"""Run the verb the way the interpreter runs it, so what is proven is the real entry point."""
		action = _action("Update Field", target_doctype=_TARGET, updates=[
			{"name": "subject", "mode": refs.LITERAL, "value": "probe"}
		], **config)
		actions._action_set_field(action, lead or self.lead, ctx or self.ctx, ("", "", ""), None)
		return actions.wrote_name(ctx or self.ctx, _TARGET)

	def _fresh_run(self):
		"""A NEW journey's context — what the second message of a conversation really arrives with."""
		return refs.Values(buckets={}).writing_as("n1")


class TestOneOpenTicketPerPatient(_ThrottleCase):
	def test_a_second_run_reuses_the_open_ticket_instead_of_raising_another(self):
		first = self._raise()
		second = self._raise(ctx=self._fresh_run())
		self.assertEqual(first, second, "a second message raised a second ticket")
		self.assertEqual(
			frappe.db.count(_TARGET, {self.spec["lead_field"]: self.lead}), 1
		)

	def test_the_patient_is_stamped_so_the_ticket_can_be_found_again(self):
		name = self._raise()
		self.assertEqual(frappe.db.get_value(_TARGET, name, self.spec["lead_field"]), self.lead)

	def test_a_finished_ticket_is_never_reopened(self):
		"""A ticket closed months ago is not where today's question belongs."""
		first = self._raise()
		closed = frappe.db.get_value(
			"HD Ticket Status", {"category": ["in", subjects.TERMINAL_CATEGORIES]}, "name"
		)
		if not closed:
			self.skipTest("this site declares no finished ticket status")
		frappe.db.set_value(_TARGET, first, "status", closed)
		self.assertNotEqual(self._raise(ctx=self._fresh_run()), first)

	def test_a_paused_ticket_is_still_the_patients_open_one(self):
		"""Paused is waiting on somebody, not finished — the next message belongs on it."""
		first = self._raise()
		frappe.db.set_value(_TARGET, first, "status", self._status("Paused"))
		self.assertEqual(self._raise(ctx=self._fresh_run()), first)

	def test_one_patients_ticket_is_never_handed_to_another(self):
		if not self.other_lead:
			self.skipTest("this site has only one lead")
		mine = self._raise()
		theirs = self._raise(lead=self.other_lead, ctx=self._fresh_run())
		self.assertNotEqual(mine, theirs)

	def test_the_node_gained_no_new_control(self):
		"""W8.1 made Update Field a node that does not change shape; reuse must not re-open that.

		Locked here as well as in `test_update_field_rows`, because the temptation to expose a
		per-fire escape hatch belongs to THIS feature and would be re-added from this file's side.
		"""
		self.assertEqual(
			[p["name"] for p in actions.VERBS["Update Field"]["params"]], ["target_doctype", "updates"]
		)

	def test_two_nodes_in_one_run_still_write_the_same_record(self):
		"""`wrote_name` is asked first and still wins — the shipped within-a-run behaviour is unchanged."""
		first = self._raise()
		self.assertEqual(self._raise(), first)


class TestNothingElseMoved(_ThrottleCase):
	"""The collateral proof. `resolve_target` is the one answer for EVERY verb and every target."""

	def test_a_write_to_the_lead_still_resolves_to_the_lead(self):
		self.assertEqual(
			actions.resolve_target(
				_action("Update Field", target_doctype=_LEAD), self.lead, None, self.ctx
			),
			(_LEAD, self.lead),
		)

	def test_a_write_to_the_triggering_record_still_resolves_to_that_record(self):
		trigger = frappe._dict(doctype="CRM Task", name="TASK-PROBE")
		self.assertEqual(
			actions.resolve_target(
				_action("Update Field", target_doctype="CRM Task"), self.lead, trigger, self.ctx
			),
			("CRM Task", "TASK-PROBE"),
		)

	def test_a_target_out_of_scope_still_raises(self):
		with self.assertRaises(ValueError):
			actions.resolve_target(
				_action("Update Field", target_doctype="User"), self.lead, None, self.ctx
			)

	def test_the_reuse_lookup_is_never_reached_for_a_lead_write(self):
		"""Belt and braces: the branch is keyed on the declared catalog, so a lead can never enter it."""
		self.assertIsNone(actions._open_record_for(_LEAD, _action("Update Field"), self.lead))
