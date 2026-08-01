# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The DOOR in front of the verifier: what `CRM Derived Field.validate` refuses to store, and how well.

`test_derived_fields` proves the verifier on declarations built in Python, and `test_derived_field_head`
proves an authored row is the same citizen a code declaration is. Neither is about the moment an operator
types JSON into a text box and presses Save, which is the only moment this feature is really used.

Two things are asserted here and nowhere else:

  * A declaration that cannot be served CANNOT BE STORED. Not stored-and-broken, not stored-and-disabled —
    refused, with nothing left behind on either side of the refusal.
  * THE REFUSAL IS USABLE. An operator who cannot see which buckets collide, and on what value, has no way
    back from a rejected save except guessing. The message is the whole safety story's last mile, so it is
    tested as a promise: the two bucket names and the column they collide on are all in it.

The shape gate is the other half. `derived.from_row` reads a stored row LENIENTLY on purpose — it is on
the hot read path and must not take a list down over one bad row — so a bucket that is not an object, a
misspelt key or a missing value passes THROUGH it. Everything it lets past, this refuses at authoring time.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.list_engine.test_derived_field_row
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.list_engine import derived
from tatva_connect.tatva_connect.doctype.crm_derived_field.crm_derived_field import _probe_defaults

TASK = "CRM Task"
ROW = "CRM Derived Field"
FIELD = "_authored_open_state"
CLOSED = ["Done", "Canceled"]

# Two buckets that partition every task and nothing else — the shape `fields.py` ships, cut down to two.
SOUND = [
	{"value": "Open", "theme": "green", "filters": [["status", "not in", CLOSED]]},
	{"value": "Closed", "theme": "gray", "filters": [["status", "in", CLOSED]]},
]

# The measured trap on frappe 16.22.0: SQL excludes the record equal to the bound, Python includes it.
INCLUSIVE = [
	{"value": "Through Today", "filters": [["due_date", "<=", derived.TOMORROW_START]]},
	{"value": "After Today", "filters": [["due_date", ">", derived.TOMORROW_START]]},
]

# `Any Status` swallows `Open`, so one record reads as two values and SQL has no first-match rule.
OVERLAPPING = [
	{"value": "Open", "filters": [["status", "not in", CLOSED]]},
	{"value": "Any Status", "filters": [["status", "is", "set"]]},
]


def _row(fieldname=FIELD, buckets=None, **overrides):
	"""An unsaved authored row. Saving it is what runs the gate, so no test here may bypass `insert`."""
	return frappe.get_doc(
		{
			"doctype": ROW,
			"dt": TASK,
			"fieldname": fieldname,
			"label": "Authored Probe",
			"buckets": json.dumps(buckets if buckets is not None else SOUND),
			**overrides,
		}
	)


class AuthoringCase(FrappeTestCase):
	"""Redis is not rolled back with the transaction, so the registry cache is dropped on both sides."""

	def setUp(self):
		frappe.set_user("Administrator")
		# Cleared, not only rolled back: `validate` opens a savepoint for `verify()`, so a row left by a
		# sibling test collides on the name before the assertion under test is ever reached.
		frappe.db.delete(derived.ROW_DOCTYPE, {"fieldname": ["like", r"\_%"]})
		derived.reload()
		self.addCleanup(derived.reload)
		self.addCleanup(frappe.db.delete, derived.ROW_DOCTYPE, {"fieldname": ["like", r"\_%"]})


class TestWhatTheRowItselfDeclares(AuthoringCase):
	"""The doctype's own defaults and naming — the parts an operator never types."""

	def test_a_row_ships_disabled(self):
		"""Dormant by default is the app's standing rule, and it has to be the FIELD's default: a row
		authored by anyone who does not think to say otherwise must change nothing anywhere."""
		row = _row()
		row.insert()
		self.assertEqual(row.enabled, 0, "the row did not ship disabled")

	def test_the_row_is_named_by_its_list_and_its_fieldname(self):
		"""One field per list per name, enforced by the name itself rather than by a uniqueness check."""
		row = _row()
		row.insert()
		self.assertEqual(row.name, f"{TASK}::{FIELD}")

	def test_the_surfaces_are_stored_the_way_they_are_read(self):
		"""A blank means every menu, and it is written back so the stored row says what it means."""
		row = _row(surfaces="  COLUMN ,\n filter ")
		row.insert()
		self.assertEqual(row.surfaces, "column, filter")
		self.assertEqual(derived.from_row(row).surfaces, ("column", "filter"))

		blank = _row(fieldname="_authored_blank_surfaces", surfaces="")
		blank.insert()
		self.assertEqual(blank.surfaces, ", ".join(derived.SURFACES))


class TestTheShapeGate(AuthoringCase):
	"""What `derived.from_row` reads past in silence, refused at authoring time instead."""

	def test_malformed_json_is_refused_readably(self):
		row = _row()
		row.buckets = '[{"value": "Open", "filters": [["status", "in", ["Done"]]]'
		with self.assertRaises(frappe.ValidationError) as refused:
			row.insert()
		self.assertIn("JSON", str(refused.exception))

	def test_buckets_that_are_not_a_list_are_refused(self):
		"""Order is load-bearing twice — the value a record shows, and the order the field sorts in."""
		with self.assertRaises(frappe.ValidationError) as refused:
			_row(buckets={"value": "Open", "filters": []}).insert()
		self.assertIn("list", str(refused.exception).lower())

	def test_a_bucket_that_is_not_an_object_is_refused(self):
		"""`from_row` DROPS one of these, so the field would silently declare fewer buckets than authored."""
		with self.assertRaises(frappe.ValidationError):
			_row(buckets=[*SOUND, "Closed"]).insert()

	def test_a_bucket_with_no_value_is_refused(self):
		"""`from_row` reads a missing value as None, and a nameless bucket is a blank cell forever."""
		with self.assertRaises(frappe.ValidationError):
			_row(buckets=[{"filters": [["status", "in", CLOSED]]}]).insert()

	def test_a_misspelt_bucket_key_is_refused(self):
		"""A key nothing reads would be stored and then ignored, which is worse than a refusal."""
		with self.assertRaises(frappe.ValidationError) as refused:
			_row(
				buckets=[{"value": "Open", "colour": "green", "filters": [["status", "in", CLOSED]]}]
			).insert()
		self.assertIn("colour", str(refused.exception))

	def test_a_bucket_with_no_filters_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			_row(buckets=[{"value": "Open", "filters": []}]).insert()

	def test_a_filter_that_is_not_a_triple_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			_row(buckets=[{"value": "Open", "filters": [["status", "in"]]}]).insert()

	def test_a_fieldname_that_is_not_a_fieldname_is_refused(self):
		"""It is a key every surface addresses — the column header, the sort, the board, the export."""
		with self.assertRaises(frappe.ValidationError):
			_row(fieldname="Due State!").insert()

	def test_a_doctype_with_no_list_is_refused(self):
		"""A Single holds one record and a child table has no list, so there is no column to appear in."""
		with self.assertRaises(frappe.ValidationError):
			_row(dt="System Settings").insert()

	def test_a_bucket_reading_a_column_the_list_does_not_have_is_refused(self):
		"""Unchecked, the typo reached SQL and answered `OperationalError: Unknown column` — a database
		error, not a refusal an operator can act on. Measured before the check existed."""
		with self.assertRaises(frappe.ValidationError) as refused:
			_row(buckets=[{"value": "Open", "filters": [["not_a_column", "=", "x"]]}]).insert()
		message = frappe.utils.strip_html(str(refused.exception))
		self.assertIn("not_a_column", message)
		self.assertIn(TASK, message)
		self.assertNotIn("OperationalError", message)

	def test_an_order_by_that_names_no_column_is_refused(self):
		with self.assertRaises(frappe.ValidationError) as refused:
			_row(order_by="not_a_column").insert()
		self.assertIn("not_a_column", str(refused.exception))

	def test_an_order_by_carrying_a_direction_is_refused(self):
		"""The caller's own direction is appended to it, so "due_date desc" would reach SQL as two."""
		with self.assertRaises(frappe.ValidationError):
			_row(order_by="due_date desc").insert()

	def test_a_fieldname_already_declared_in_code_is_refused(self):
		"""Code wins the merge, so a row shadowing `fields.py` would store, enable and never be served.

		Declares its OWN code field rather than leaning on `due_state`: that one is being moved into a row,
		and a test resting on where a declaration happens to live today proves nothing tomorrow."""
		shadowed = "_code_declared_probe"
		derived.register(
			derived.DerivedField(
				doctype=TASK,
				fieldname=shadowed,
				label="Code Probe",
				buckets=[derived.Bucket("Open", [("status", "not in", CLOSED)])],
			)
		)
		self.addCleanup(derived._REGISTRY.get(TASK, {}).pop, shadowed, None)
		with self.assertRaises(frappe.ValidationError) as refused:
			_row(fieldname=shadowed).insert()
		self.assertIn(shadowed, str(refused.exception))


class TestTheRefusalIsUsable(AuthoringCase):
	"""An operator has only the message to work from. It names the buckets AND the value they collide on."""

	def test_an_overlap_names_both_buckets_and_the_column_it_happens_on(self):
		"""`verify()` reports the probe RECORD an overlap was found on, and that record is rolled back
		before the message is built — so its name means nothing, and the values have to be recovered."""
		with self.assertRaises(frappe.ValidationError) as refused:
			_row(buckets=OVERLAPPING).insert()
		message = str(refused.exception)
		self.assertIn("Open", message, "the refusal does not name the first colliding bucket")
		self.assertIn("Any Status", message, "the refusal does not name the second colliding bucket")
		self.assertIn("status", message, "the refusal never names the column the collision is on")

	def test_an_inclusive_datetime_bound_saves(self):
		"""It was refused until 2026-08-01, and the refusal was this layer reporting its own bug.

		Measured against rows at bound-1s, bound and bound+1s: with the operand left as a bare string SQL
		returned one row and Python two — SQL dropped the record sitting exactly on the bound, and two is
		the right answer. Typed from the column, both readers return two, so there is nothing to refuse.
		Refusing it now would need a second arbiter beside `verify()`, which is the thing this doctype
		does not have."""
		_row(buckets=INCLUSIVE, fieldname="_probe_inclusive_ok").insert()


class TestAuthoringLeavesNothingBehind(AuthoringCase):
	"""`verify()` inserts real records to prove a declaration. Both outcomes must be as clean as no save."""

	def test_a_successful_save_leaves_no_records_behind(self):
		before = frappe.db.count(TASK)
		_row(enabled=1).insert()
		self.assertEqual(frappe.db.count(TASK), before, "probe records outlived the savepoint")

	def test_a_refused_save_leaves_no_records_and_no_row_behind(self):
		before = frappe.db.count(TASK)
		with self.assertRaises(frappe.ValidationError):
			_row(buckets=OVERLAPPING).insert()
		self.assertEqual(frappe.db.count(TASK), before, "probe records outlived a refusal")
		self.assertFalse(frappe.db.exists(ROW, {"dt": TASK, "fieldname": FIELD}), "a refused row was stored")


class TestProbeDefaults(FrappeTestCase):
	"""The trial records are generated from the doctype's own meta, never from a table of doctypes here —
	the operator chooses the list, so no list of ours can be enumerated in code."""

	def test_a_mandatory_column_with_no_default_is_filled(self):
		self.assertEqual(_probe_defaults(TASK).get("title"), "Derived Field Probe")

	def test_a_column_that_is_not_mandatory_is_left_alone(self):
		"""Filling an optional column would put every probe record off the boundary it was built for."""
		defaults = _probe_defaults(TASK)
		self.assertNotIn("status", defaults)
		self.assertNotIn("due_date", defaults)
