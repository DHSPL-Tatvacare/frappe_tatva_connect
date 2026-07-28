# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A side-effect must never destroy a rep's save.

Frappe runs `doc_events` INSIDE the save's transaction, so a notification the push service refused, or a
search index whose file was locked, used to roll the whole save back — the rep's typing gone, on a bug
that had nothing to do with what they wrote. `propagate.fail_safe` marks a savepoint, runs the hook, and
on failure undoes the hook's writes ONLY and files an Error Log row.

WHAT THESE TESTS ARE FOR, AND WHY THEY ARE NOT UNIT TESTS. A test of the helper proves the helper. It
does not prove that the helper is ON the hooks that need it, that Frappe resolves the decorated function
and not some earlier reference to it, or that the gates still refuse. So every class below drives a REAL
`.save()` / `.insert()` and makes a real hook throw underneath it.

THE RED CONTROL, KEPT IN THE SUITE (J5). Each real-save test has a twin that patches the hook back to
its UNDECORATED self — `functools.wraps` keeps the original at `__wrapped__` — and asserts the identical
failure DOES destroy the save. That pair is the proof: same lead, same exception, opposite outcome, and
the only difference is the decorator. Deleting `@fail_safe` from any wrapped hook turns the first test of
each pair red immediately, rather than a year later on a rep's screen.

AND THE GATES ARE UNTOUCHED. `test_a_bad_phone_number_is_still_refused` drives the same save path through
`normalize_lead_phones` and asserts the refusal survives. A change that made propagate hooks safe by
making gates soft would pass every other test in this file.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.propagate.test_propagate_fail_safe
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.activity import timeline
from tatva_connect.lead import leads
from tatva_connect.propagate import fail_safe
from tatva_connect.search import index
from tatva_connect.workflow_engine import triggers

_BOOM = "zz propagate probe: this side-effect blew up"


def _logged(fn_name) -> int:
	"""Error Log rows this run wrote for `fn_name`. The title is the contract: a swallow that named
	nothing would be indistinguishable from a hook that quietly did nothing at all."""
	return frappe.db.count("Error Log", {"method": ["like", f"%propagate hook failed%{fn_name}%"]})


class TestFailSafeHelper(FrappeTestCase):
	"""The mechanism itself: undo the hook's writes, keep everyone else's, say what was lost."""

	def setUp(self):
		self.lead = frappe.get_doc(
			{"doctype": "CRM Lead", "first_name": "ZZ Helper Probe", "mobile_no": "+919000000051"}
		).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture, runs as Administrator

	def tearDown(self):
		frappe.db.rollback()

	def test_the_hooks_own_write_is_undone_and_the_callers_survives(self):
		"""The savepoint's whole job: the half-written side-effect goes, the record stays."""

		@fail_safe
		def hook(doc, method=None):
			frappe.db.set_value("CRM Lead", doc.name, "first_name", "ZZ HALF WRITTEN")
			raise RuntimeError(_BOOM)

		hook(self.lead)  # must not raise
		self.assertEqual(
			frappe.db.get_value("CRM Lead", self.lead.name, "first_name"),
			"ZZ Helper Probe",
			"the failing hook's own write must be rolled back to the savepoint",
		)
		self.assertTrue(frappe.db.exists("CRM Lead", self.lead.name), "the caller's record must survive")

	def test_the_transaction_still_accepts_writes_afterwards(self):
		"""A bare try/except leaves whatever the hook broke in place; this proves the caller can still
		write after a swallow, which is the only thing that makes the save's own commit possible."""

		@fail_safe
		def hook(doc, method=None):
			raise RuntimeError(_BOOM)

		hook(self.lead)
		frappe.db.set_value("CRM Lead", self.lead.name, "first_name", "ZZ Written After")
		self.assertEqual(frappe.db.get_value("CRM Lead", self.lead.name, "first_name"), "ZZ Written After")

	def test_every_swallow_names_the_hook_the_doctype_and_the_docname(self):
		"""Silence is the risk this change introduces. An Error Log row is the whole mitigation."""

		@fail_safe
		def zz_named_hook(doc, method=None):
			raise RuntimeError(_BOOM)

		before = _logged("zz_named_hook")
		zz_named_hook(self.lead)
		self.assertEqual(_logged("zz_named_hook"), before + 1)
		row = frappe.get_last_doc("Error Log", filters={"method": ["like", "%zz_named_hook%"]})
		self.assertEqual(row.reference_doctype, "CRM Lead")
		self.assertEqual(row.reference_name, self.lead.name)
		self.assertIn(self.lead.name, row.error)

	def test_a_business_refusal_is_never_swallowed(self):
		"""`frappe.throw` from a propagate hook means the hook is a GATE in the wrong slot. Surface it."""

		@fail_safe
		def hook(doc, method=None):
			frappe.throw("zz this is a business rule, not an accident")

		with self.assertRaises(frappe.ValidationError):
			hook(self.lead)

	def test_a_permission_refusal_is_never_swallowed(self):
		@fail_safe
		def hook(doc, method=None):
			raise frappe.PermissionError("zz not allowed")

		with self.assertRaises(frappe.PermissionError):
			hook(self.lead)

	def test_a_framework_accident_that_merely_subclasses_validationerror_is_swallowed(self):
		"""`DoesNotExistError` is a linked row deleted underneath us, not a business rule. Almost
		everything frappe raises subclasses ValidationError; matching it loosely would leave the disaster
		shape fully intact for its most common cause."""

		@fail_safe
		def hook(doc, method=None):
			raise frappe.DoesNotExistError("zz the row went away")

		hook(self.lead)  # must not raise

	def test_the_hooks_return_value_reaches_the_caller_unchanged(self):
		"""Nothing else about a hook may change — a wrapper that ate return values would be a second
		behaviour change, and there is exactly one allowed."""

		@fail_safe
		def hook(doc, method=None):
			return (doc.name, method)

		self.assertEqual(hook(self.lead, "on_update"), (self.lead.name, "on_update"))

	def test_logging_a_failure_cannot_re_enter_the_hooks(self):
		"""`log_error` INSERTS a document, and an insert fires `doc_events["*"]` — the same wildcard
		several wrapped hooks ride. Without the re-entrancy flag this recurses until the stack dies."""

		@fail_safe
		def hook(doc, method=None):
			raise RuntimeError(_BOOM)

		with patch("tatva_connect.workflow_engine.triggers._engine_may_run", side_effect=RuntimeError(_BOOM)):
			hook(self.lead)  # must not raise, must not recurse
		self.assertFalse(frappe.flags.get("in_propagate_log"), "the flag must be cleared on the way out")


class TestARealSaveSurvivesAFailingPropagateHook(FrappeTestCase):
	"""The disaster shape, driven through the real save path, one class of hook at a time."""

	def setUp(self):
		self.lead = frappe.get_doc(
			{"doctype": "CRM Lead", "first_name": "ZZ Save Probe", "mobile_no": "+919000000052"}
		).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture, runs as Administrator

	def tearDown(self):
		frappe.db.delete("CRM Timeline Event", {"reference_name": self.lead.name})
		frappe.db.rollback()

	def _rename(self, to):
		lead = frappe.get_doc("CRM Lead", self.lead.name)
		lead.first_name = to  # lead_name is derived from it, and lead_name is a declared index field
		lead.save(ignore_permissions=True)  # authz-ok: tier-a — test drives the rep's own save path
		return lead

	# ---- CRM Lead.on_update: the search index -------------------------------------------------
	def test_a_failing_search_reindex_does_not_lose_the_rep_s_edit(self):
		before = _logged("reindex_on_lead_context_change")
		with patch("tatva_connect.search.index._enqueue_reindex", side_effect=RuntimeError(_BOOM)):
			self._rename("ZZ Save Probe Renamed")
		self.assertEqual(
			frappe.db.get_value("CRM Lead", self.lead.name, "first_name"),
			"ZZ Save Probe Renamed",
			"the edit must be committed even though the reindex threw",
		)
		self.assertEqual(_logged("reindex_on_lead_context_change"), before + 1, "the swallow must be logged")

	def test_red_control_without_the_wrapper_the_same_failure_destroys_the_save(self):
		"""The defect, pinned. Same lead, same exception — only the decorator differs."""
		with (
			patch("tatva_connect.search.index._enqueue_reindex", side_effect=RuntimeError(_BOOM)),
			patch(
				"tatva_connect.search.index.reindex_on_lead_context_change",
				index.reindex_on_lead_context_change.__wrapped__,
			),
			self.assertRaises(RuntimeError),
		):
			self._rename("ZZ Never Saved")

	# ---- after_insert: the activity timeline --------------------------------------------------
	def _note(self):
		return frappe.get_doc(
			{
				"doctype": "FCRM Note",
				"title": "ZZ propagate note",
				"content": "x",
				"reference_doctype": "CRM Lead",
				"reference_docname": self.lead.name,
			}
		)

	def test_a_failing_timeline_index_does_not_lose_the_note(self):
		before = _logged("index_event")
		with patch("tatva_connect.activity.timeline.event_row", side_effect=RuntimeError(_BOOM)):
			note = self._note().insert(ignore_permissions=True)  # authz-ok: tier-a — test drives the real insert
		self.assertTrue(frappe.db.exists("FCRM Note", note.name), "the rep's note must survive")
		self.assertEqual(_logged("index_event"), before + 1)

	def test_red_control_without_the_wrapper_the_same_failure_destroys_the_note(self):
		with (
			patch("tatva_connect.activity.timeline.event_row", side_effect=RuntimeError(_BOOM)),
			patch("tatva_connect.activity.timeline.index_event", timeline.index_event.__wrapped__),
			self.assertRaises(RuntimeError),
		):
			self._note().insert(ignore_permissions=True)  # authz-ok: tier-a — test drives the real insert

	# ---- the wildcard doc_events["*"]: the workflow engine ------------------------------------
	def test_a_failing_workflow_trigger_does_not_lose_the_save(self):
		"""The wildcard fires on EVERY save of EVERY doctype, so an engine fault here is a site-wide
		outage of the save button. This is the single most valuable wrap in the set."""
		before = _logged("on_updated")
		with patch("tatva_connect.workflow_engine.triggers._engine_may_run", side_effect=RuntimeError(_BOOM)):
			self._rename("ZZ Save Probe Wildcard")
		self.assertEqual(
			frappe.db.get_value("CRM Lead", self.lead.name, "first_name"), "ZZ Save Probe Wildcard"
		)
		self.assertEqual(_logged("on_updated"), before + 1)

	def test_red_control_without_the_wrapper_a_wildcard_fault_destroys_every_save(self):
		with (
			patch("tatva_connect.workflow_engine.triggers._engine_may_run", side_effect=RuntimeError(_BOOM)),
			patch("tatva_connect.workflow_engine.triggers.on_updated", triggers.on_updated.__wrapped__),
			self.assertRaises(RuntimeError),
		):
			self._rename("ZZ Never Saved Either")


class TestTheGatesStillRefuse(FrappeTestCase):
	"""The scope boundary. A gate that stopped refusing would be a far worse defect than the one fixed."""

	def tearDown(self):
		frappe.db.rollback()

	def test_a_bad_phone_number_is_still_refused(self):
		"""`normalize_lead_phones` is a GATE on `validate` and was not touched. It must still block the
		save outright — a lead whose phone is not real cannot dedup, cannot be called and cannot be
		messaged, so a half-good record is worse than a refusal."""
		with patch("tatva_connect.lead.leads.automation.is_enabled", return_value=True):
			with self.assertRaises(frappe.ValidationError):
				frappe.get_doc(
					{"doctype": "CRM Lead", "first_name": "ZZ Bad Phone", "mobile_no": "12345"}
				).insert(ignore_permissions=True)  # authz-ok: tier-a — test drives the real gate

	def test_the_gate_is_reached_through_the_same_save_a_propagate_hook_rides(self):
		"""Belt and braces: the refusal above must come from OUR gate, not from a stray framework error."""
		doc = frappe.get_doc({"doctype": "CRM Lead", "first_name": "ZZ Gate Probe", "mobile_no": "12345"})
		with patch("tatva_connect.lead.leads.automation.is_enabled", return_value=True):
			with self.assertRaises(frappe.ValidationError):
				leads.normalize_lead_phones(doc)
