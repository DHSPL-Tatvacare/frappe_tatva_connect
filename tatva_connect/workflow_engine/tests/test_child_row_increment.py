# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
""""Increment by" on a singleton child-row counter via Upsert Child Row — and the silent no-op that guarded it.

THE SILENT NO-OP. Update Field can name a field that lives on a child table. can_set=0 was the only
guard: tick it and the write silently does nothing — no exception, no log, journey reports success.
_prove_silent_noop_is_closed proves the guard is in place.

UPSERT CHILD ROW SINGLETON. The metrics section (plan, care too) has is_multi_row=0 and no row_key_field.
Upsert Child Row was built for keyed multi-row tables; on a singleton the allowlist gate demands a row key
that doesn't exist. The fix auto-resolves the one row. test_increment_adds_to_a_singleton_child_row is RED
on the old code.

SINGLETON INVARIANT. A non-empty match against a singleton table raises. An empty match against a keyed
table still raises. Two concurrent writes on an empty singleton must produce ONE row, not two.

Run:
    cd /home/frappe/frappe-bench/sites && ../env/bin/python run.py tatva_connect.workflow_engine.tests.test_child_row_increment
"""
import json
import threading
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import actions
from tatva_connect.tests.authz.grains import assert_masters_exist
from tatva_connect.tests.automation import field_allowlist
from tatva_connect.workflow_engine import refs
from tatva_connect.workflow_engine.tests import fixtures as fx

_CHILD_TABLE = "custom_lead_activity_metrics"
_CHILD_DT = "CRM Lead Activity Metrics"
_COUNTER = "custom_rnr_count"


def _reread_child_counter(lead_name):
	return frappe.db.get_value(
		_CHILD_DT, {"parent": lead_name, "parentfield": _CHILD_TABLE}, _COUNTER,
	)


class _ChildCounterBase(FrappeTestCase):
	"""One lead whose metrics row carries a KNOWN committed counter, and the allowlist ticked."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		assert_masters_exist()
		fx.arm_engine(True, cls)
		field_allowlist.seed_settable(
			"CRM Lead", _COUNTER,
			vertical=fx.GRAIN["vertical"], group=fx.GRAIN["group"], program=fx.GRAIN["program"],
		)
		cls.lead = fx.make_lead()
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.delete_doc("CRM Lead", cls.lead.name, force=True, ignore_permissions=True)
		field_allowlist.clear("CRM Lead")
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		super().setUp()
		_ensure_metrics_row(self.lead.name, counter=2)
		self.addCleanup(_clear_metrics_rows, self.lead.name)


# -- silent no-op proof --------------------------------------------------------------------


class TestUpdateFieldRaisesOnChildRowFields(_ChildCounterBase):

	def test_prove_silent_noop_is_closed(self):
		"""RED on today's code. custom_rnr_count is on the metrics child table, not CRM Lead.
		Update Field must refuse it — either through is_settable (can_set gate) or through the
		meta check when is_settable is bypassed. A silent no-op is a journey that reports success
		while writing nothing."""
		with self.assertRaises((PermissionError, ValueError)) as raised:
			actions._action_set_field(
				frappe._dict(action_type="Update Field", target_doctype="CRM Lead", updates=[
					{"name": _COUNTER, "mode": refs.LITERAL, "value": "5"},
				]),
				self.lead.name, {}, fx.AXES, None,
			)
		self.assertIn(_COUNTER, str(raised.exception))


# -- Upsert Child Row + singleton INCREMENT -------------------------------------------------


def _singleton_upsert(lead_name, field, mode, value):
	"""An Upsert Child Row action on a singleton — empty match, auto-resolved row."""
	return frappe._dict(action_type="Upsert Child Row", child_table=_CHILD_TABLE,
	                    match_json="{}", set_json=json.dumps({field: {"mode": mode, "value": str(value)}}))


class TestIncrementOnASingletonChildRow(_ChildCounterBase):

	def test_increment_adds_to_the_child_row_counter(self):
		"""RED on the old code. _assert_child_allowlisted demanded a row key that doesn't exist
		on the singleton metrics section, so every Upsert died with PermissionError."""
		before = _reread_child_counter(self.lead.name)
		self.assertEqual(before, 2, "premise: the counter must start at a known value")

		actions._action_upsert_child(
			_singleton_upsert(self.lead.name, _COUNTER, refs.INCREMENT, 1),
			self.lead.name, {}, fx.AXES, None,
		)

		self.assertEqual(_reread_child_counter(self.lead.name), 3)

	def test_three_increments_of_one_land_at_five(self):
		for _ in range(3):
			actions._action_upsert_child(
				_singleton_upsert(self.lead.name, _COUNTER, refs.INCREMENT, 1),
				self.lead.name, {}, fx.AXES, None,
			)
		self.assertEqual(_reread_child_counter(self.lead.name), 5)

	def test_literal_set_still_works(self):
		"""Plain values (no mode dict) still work alongside mode dicts."""
		self.assertNotEqual(_reread_child_counter(self.lead.name), 9,
		                    "premise: the counter must not already hold the value being set")
		actions._action_upsert_child(
			frappe._dict(action_type="Upsert Child Row", child_table=_CHILD_TABLE,
			             match_json="{}", set_json=json.dumps({_COUNTER: "9"})),
			self.lead.name, {}, fx.AXES, None,
		)
		self.assertEqual(_reread_child_counter(self.lead.name), 9)

	def test_increment_from_absent_starts_at_the_value(self):
		"""No row → INCREMENT starts from 0 → flt(0) + flt(1) = 1."""
		_clear_metrics_rows(self.lead.name)
		actions._action_upsert_child(
			_singleton_upsert(self.lead.name, _COUNTER, refs.INCREMENT, 1),
			self.lead.name, {}, fx.AXES, None,
		)
		self.assertEqual(_reread_child_counter(self.lead.name), 1)

	def test_a_typoed_mode_raises(self):
		"""Incremnt is not a mode — resolve_row would fall through to return value, setting the
		counter to 1 instead of incrementing. The typo must be a loud refusal."""
		with self.assertRaises(ValueError) as raised:
			actions._action_upsert_child(
				frappe._dict(action_type="Upsert Child Row", child_table=_CHILD_TABLE,
				             match_json="{}", set_json=json.dumps({_COUNTER: {"mode": "Incremnt", "value": "1"}})),
				self.lead.name, {}, fx.AXES, None,
			)
		self.assertIn("Incremnt", str(raised.exception))


# -- singleton invariants -----------------------------------------------------------------


class TestSingletonInvariants(_ChildCounterBase):

	def test_a_non_empty_match_against_a_singleton_refuses_loudly(self):
		"""A singleton has no row key — any match is an author error, never silently ignored."""
		with self.assertRaises(ValueError) as raised:
			actions._action_upsert_child(
				frappe._dict(action_type="Upsert Child Row", child_table=_CHILD_TABLE,
				             match_json=json.dumps({"x": "y"}), set_json="{}"),
				self.lead.name, {}, fx.AXES, None,
			)
		self.assertIn("singleton", str(raised.exception))

	def test_an_empty_match_against_a_keyed_table_still_refuses(self):
		"""The keyed path is untouched — empty match_json must still be refused for multi-row tables."""
		with self.assertRaises(ValueError) as raised:
			actions._action_upsert_child(
				frappe._dict(action_type="Upsert Child Row", child_table="custom_acquisition_profile",
				             match_json="{}", set_json=json.dumps({"utm_source": "x"})),
				self.lead.name, {}, fx.AXES, None,
			)
		self.assertIn("non-empty", str(raised.exception))

	def test_a_singleton_with_two_rows_raises(self):
		"""rows[0] silently hid 941 corrupted rows. A singleton with two rows must fail loud."""
		tdoc = frappe.get_doc("CRM Lead", self.lead.name)
		tdoc.append(_CHILD_TABLE, {_COUNTER: 99})
		tdoc.save(ignore_permissions=True)  # authz-ok: tier-c — test fixture
		with self.assertRaises(ValueError) as raised:
			actions._action_upsert_child(
				_singleton_upsert(self.lead.name, _COUNTER, refs.LITERAL, "5"),
				self.lead.name, {}, fx.AXES, None,
			)
		self.assertIn("has 2 rows", str(raised.exception))


# -- concurrency proof --------------------------------------------------------------------


class TestTwoSingletonUpsertsOnOneLeadBothLand(_ChildCounterBase):
	"""The same concurrency guard as Update Field. Driven with a barrier, not reasoned about."""

	def _race(self, barrier=None):
		"""Two upserts incrementing the same counter on their own connections. Same shape as
		test_update_field_rows._race — barrier inside _resolve_write_target, frappe.init per thread."""
		site = frappe.local.site
		original = actions._resolve_write_target
		raised = []

		def barriered(*args, **kwargs):
			doc = original(*args, **kwargs)
			if barrier is not None:
				try:
					barrier.wait()
				except threading.BrokenBarrierError:
					pass
			return doc

		def journey():
			frappe.init(site=site)
			frappe.connect()
			try:
				frappe.db.begin()
				actions._action_upsert_child(
					_singleton_upsert(self.lead.name, _COUNTER, refs.INCREMENT, 1),
					self.lead.name, {}, fx.AXES, None,
				)
				frappe.db.commit()
			except Exception as e:
				raised.append(e)
			finally:
				frappe.destroy()

		with patch.object(actions, "_resolve_write_target", barriered):
			threads = [threading.Thread(target=journey) for _ in range(2)]
			for t in threads:
				t.start()
			for t in threads:
				t.join(timeout=60)
		return raised

	def test_the_lock_serializes_and_both_land_on_re_drive(self):
		"""One barrier, two threads, one row. The for_update lock makes the second thread block until
		the first commits. The loser's snapshot predates the winner's commit, so its increment is lost —
		but re-driven, it reads what the first wrote and the counter lands at 2."""
		_clear_metrics_rows(self.lead.name)
		barrier = threading.Barrier(2, timeout=4)
		raised = self._race(barrier=barrier)

		self.assertTrue(barrier.broken,
		                "both threads met at the barrier — the row was never locked")
		self.assertLessEqual(len(raised), 1,
		                     "both threads failed — nothing serialized")

		for _ in raised:
			actions._action_upsert_child(
				_singleton_upsert(self.lead.name, _COUNTER, refs.INCREMENT, 1),
				self.lead.name, {}, fx.AXES, None,
			)

		rows = frappe.get_all(_CHILD_DT, filters={"parent": self.lead.name, "parentfield": _CHILD_TABLE})
		self.assertEqual(len(rows), 1, "two singleton writes must produce one row, not two")
		self.assertEqual(_reread_child_counter(self.lead.name), 2,
		                 "two increments must add 2, not 1")


# -- helpers ------------------------------------------------------------------------------


def _ensure_metrics_row(lead_name, counter=0):
	"""One metrics child row on the lead, carrying the counter at a known value."""
	existing = _existing_metrics_row(lead_name)
	if existing:
		frappe.db.set_value(_CHILD_DT, existing, _COUNTER, counter)
		frappe.db.commit()
		return existing
	doc = frappe.get_doc({
		"doctype": _CHILD_DT,
		"parent": lead_name,
		"parentfield": _CHILD_TABLE,
		"parenttype": "CRM Lead",
		_COUNTER: counter,
	}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture
	frappe.db.commit()
	return doc.name


def _existing_metrics_row(lead_name):
	return frappe.db.get_value(
		_CHILD_DT, {"parent": lead_name, "parentfield": _CHILD_TABLE}, "name",
	)


def _clear_metrics_rows(lead_name):
	for name in frappe.get_all(
		_CHILD_DT, filters={"parent": lead_name, "parentfield": _CHILD_TABLE}, pluck="name",
	):
		frappe.delete_doc(_CHILD_DT, name, force=True, ignore_permissions=True)
	frappe.db.commit()
