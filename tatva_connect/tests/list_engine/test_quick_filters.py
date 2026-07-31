# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The quick-filter bar carries a derived field where the REP put it, and nowhere else.

The bar is not a lens. Which fields get a control is a rep's stored choice — one `CRM Global Settings`
row (`dt`, type `Quick Filters`, a json list of fieldnames) — and native resolves each chosen name
through `frappe.get_meta` (crm/api/doc.py:178), which can never find a derived field. Appending our
answer to native's therefore looked right and was three defects:

  * the rep's chosen POSITION was unreachable — the field always landed last;
  * a REMOVAL was undone on the next load, because a removal is simply a shorter stored list and the
    append put the field straight back;
  * with NO stored row at all, native falls back to `in_standard_filter` — a DocField flag a derived
    field cannot carry — so the append made it permanently applied on a fresh site, which is exactly
    what this app's standing rule against default-on forbids.

And the write path stamped a `Property Setter` for `CRM Task-due_state-in_standard_filter`: a row
describing a property of a column that does not exist, fired unasked because the client seeds its list
from this endpoint's own answer (ViewControls.vue:853-859).

Both endpoints are resolved through `frappe.override_whitelisted_method`, exactly as an HTTP request
resolves them, so these tests prove the `hooks.py` wiring and not merely that the module exists. What
must NOT move is asserted by running native itself and comparing, never by reasoning about the path.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.list_engine.test_quick_filters
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.list_engine import derived, engine

TASK = "CRM Task"
LEAD = "CRM Lead"
FIELD = "due_state"
SETTINGS = "CRM Global Settings"

READ = "crm.api.doc.get_quick_filters"
WRITE = "crm.api.doc.update_quick_filters"

# The probe writes rows a rollback undoes; a savepoint keeps two runs of the same call independent.
_SAVEPOINT = "quick_filter_probe"


def _dispatched(cmd):
	"""The function an HTTP call to `cmd` would actually run — the override when one is registered."""
	return frappe.get_attr(frappe.override_whitelisted_method(cmd))


def _native(cmd):
	"""The upstream function, imported directly, never through the override map."""
	return frappe.get_attr(cmd)


def _store(doctype, chosen):
	"""The one row both readers read, holding exactly `chosen`. `None` leaves no row — a fresh site."""
	frappe.db.delete(SETTINGS, {"dt": doctype, "type": "Quick Filters"})
	if chosen is None:
		return
	frappe.get_doc(
		{"doctype": SETTINGS, "dt": doctype, "type": "Quick Filters", "json": json.dumps(chosen)}
	).insert()


def _stored(doctype):
	value = frappe.db.get_value(SETTINGS, {"dt": doctype, "type": "Quick Filters"}, "json")
	return frappe.parse_json(value or "[]")


def _setters(doctype):
	"""Every `in_standard_filter` Property Setter on a doctype — what the write path leaves behind."""
	return sorted(
		(row.field_name, str(row.value))
		for row in frappe.get_all(
			"Property Setter",
			filters={"doc_type": doctype, "property": "in_standard_filter"},
			fields=["field_name", "value"],
		)
	)


def _names(offered):
	return [f.get("fieldname") for f in offered]


def _stable(offered):
	"""A byte-comparable form of an answer — order and every key preserved."""
	return json.dumps(offered, sort_keys=True, default=str)


def _applied(fn, doctype, chosen, previous):
	"""Run one writer and report everything it wrote, then undo it so the next run starts level."""
	frappe.db.savepoint(_SAVEPOINT)
	try:
		fn(json.dumps(chosen), json.dumps(previous), doctype)
		return _setters(doctype), _stored(doctype)
	finally:
		frappe.db.rollback(save_point=_SAVEPOINT)


class QuickFilterBarCase(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")

	def tearDown(self):
		# A Property Setter write clears and rebuilds meta; a savepoint rollback does not put the cache back.
		frappe.clear_cache(doctype=TASK)
		frappe.clear_cache(doctype=LEAD)

	def test_a_stored_choice_puts_it_where_the_rep_put_it(self):
		"""The whole of item #8. The rep's list is the order, and ours is resolved INTO it: the derived entry
		is the ONE description `engine` gives every menu, and every real name is still native's own entry,
		re-described by nothing."""
		_store(TASK, ["status", FIELD, "priority"])
		offered = _dispatched(READ)(TASK)

		self.assertEqual(_names(offered), ["status", FIELD, "priority"])
		declared = next(f for f in engine.quick_filter_fields(TASK) if f["fieldname"] == FIELD)
		self.assertEqual(offered[1], declared)
		real = [f for f in offered if f.get("fieldname") != FIELD]
		self.assertEqual(_stable(real), _stable(_native(READ)(TASK)))

	def test_a_stored_choice_that_omits_it_does_not_get_it_back(self):
		"""Item #9. A removal IS a shorter stored list — `ViewControls.vue:774-788` drops it, doc.py:229
		writes the shorter list, and the next load must not put it back."""
		_store(TASK, ["status", "priority"])
		offered = _dispatched(READ)(TASK)

		self.assertNotIn(FIELD, _names(offered))
		self.assertEqual(_stable(offered), _stable(_native(READ)(TASK)))

	def test_no_stored_row_at_all_never_shows_it(self):
		"""Item #10. Absent a row native selects on `in_standard_filter`, which no derived field can carry,
		so a derived field is OFFERED by the picker and NOT APPLIED here. Dormant until a rep asks."""
		_store(TASK, None)
		offered = _dispatched(READ)(TASK)

		self.assertNotIn(FIELD, _names(offered))
		self.assertEqual(_stable(offered), _stable(_native(READ)(TASK)))

	def test_choosing_it_records_the_choice_and_writes_no_property_setter(self):
		"""Item #12. The choice is recorded — that row is what the reader resolves against, so the field
		appears in the bar at once — but nothing describes a property of a column that does not exist."""
		_store(TASK, ["status"])
		_dispatched(WRITE)(json.dumps(["status", FIELD]), json.dumps(["status"]), TASK)

		self.assertNotIn(FIELD, [name for name, _ in _setters(TASK)])
		self.assertEqual(_stored(TASK), ["status", FIELD])
		self.assertEqual(_names(_dispatched(READ)(TASK)), ["status", FIELD])

	def test_a_real_fieldname_writes_exactly_what_native_writes(self):
		"""The line. Our writer, given a save that adds the derived name AND removes a real one, leaves the
		Property Setters native leaves when handed the same save without the derived name — the removal
		write included. Only the recorded choice differs, and it must, because the rep chose it."""
		self.assertIsNot(_dispatched(WRITE), _native(WRITE), "the write path is not ours at all")
		_store(TASK, ["status", "priority"])
		before = ["status", "priority"]

		ours, ours_choice = _applied(_dispatched(WRITE), TASK, ["priority", FIELD], before)
		theirs, their_choice = _applied(_native(WRITE), TASK, ["priority"], before)

		self.assertEqual(ours, theirs, "a real fieldname no longer writes what native writes")
		self.assertIn(("status", "0"), ours, "the removal write is native's and must still happen")
		self.assertEqual(ours_choice, ["priority", FIELD])
		self.assertEqual(their_choice, ["priority"])

	def test_another_doctype_is_untouched_on_both_paths(self):
		"""No cross-impact. CRM Lead declares no derived field, so both endpoints are native's answer and
		native's writes — asserted by running native itself, with a stored row and without."""
		self.assertIsNot(_dispatched(WRITE), _native(WRITE), "the write path is not ours at all")
		before = ["status", "lead_name"]

		for chosen in (None, before):
			with self.subTest(stored=chosen):
				_store(LEAD, chosen)
				read = _dispatched(READ)(LEAD)
				self.assertEqual(_stable(read), _stable(_native(READ)(LEAD)))
				self.assertEqual(
					_applied(_dispatched(WRITE), LEAD, ["lead_name"], before),
					_applied(_native(WRITE), LEAD, ["lead_name"], before),
				)
