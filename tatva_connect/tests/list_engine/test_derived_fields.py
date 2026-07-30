# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A derived field is admissible if and only if `verify()` proves it — this asserts the verifier works.

The layer's whole guarantee is that frappe's two readers of one filter list name the same rows:
`get_list(filters=...)` in SQL and `evaluate_filters(row, ...)` in Python. Where they disagree the
failure is silent — a row displays in a bucket the filter will never return.

That disagreement space cannot be enumerated by reasoning; it was tried, and it produced one real find
and two false ones. So there is no operator allowlist to test here. There is one mechanism, and these
are the properties it must have:

  * it CATCHES a real disagreement — `test_the_verifier_catches_an_inclusive_datetime_bound` declares
    the exact `<=` bound measured on frappe 16.22.0 and requires the verifier to report it;
  * it CATCHES an overlap, because SQL has no first-match ordering to save one;
  * it PASSES a sound declaration, so it is not merely paranoid;
  * and it runs over the REGISTRY, so every field ever declared is proven without anyone remembering.

The registry sweep is empty until Phase 2 declares `due_state`, and that is deliberate: the guard is in
place before the first field, not after it.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.list_engine.test_derived_fields
"""

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, get_datetime

from tatva_connect.list_engine import derived

TASK = "CRM Task"
CLOSED = ["Done", "Canceled"]
DEFAULTS = {TASK: {"title": "DerivedProbe"}}


def _defaults_for(doctype):
	"""The mandatory columns a probe row needs that its declaration does not set, per doctype."""
	return DEFAULTS.get(doctype, {})


def _sound_field(fieldname="_probe_due_state"):
	"""The five-bucket shape `due_state` will ship with, under a probe name so these tests can never
	depend on — or disturb — the real declaration. Every range is half-open."""
	return derived.DerivedField(
		doctype=TASK,
		fieldname=fieldname,
		label="Probe Due State",
		order_by="due_date asc",
		buckets=[
			derived.Bucket(
				"Overdue",
				[
					("status", "not in", CLOSED),
					("due_date", "is", "set"),
					("due_date", "<", derived.NOW),
				],
			),
			derived.Bucket(
				"Due Today",
				[
					("status", "not in", CLOSED),
					("due_date", ">=", derived.NOW),
					("due_date", "<", derived.TOMORROW_START),
				],
			),
			derived.Bucket(
				"Upcoming",
				[("status", "not in", CLOSED), ("due_date", ">=", derived.TOMORROW_START)],
			),
			derived.Bucket(
				"No Due Date",
				[("status", "not in", CLOSED), ("due_date", "is", "not set")],
			),
			derived.Bucket("History", [("status", "in", CLOSED)]),
		],
	)


class TestDerivedFieldVerifier(FrappeTestCase):
	"""The spine: a declaration is admissible iff the verifier says so, on real rows."""

	def test_a_sound_declaration_verifies_clean(self):
		self.assertEqual(derived.verify(_sound_field(), defaults=_defaults_for(TASK)), [])

	def test_the_verifier_catches_an_inclusive_datetime_bound(self):
		# The measured trap: SQL excludes the row equal to the bound, Python includes it, silently.
		field = derived.DerivedField(
			doctype=TASK,
			fieldname="_probe_inclusive",
			label="Probe",
			buckets=[
				derived.Bucket("Through Today", [("due_date", "<=", derived.TOMORROW_START)]),
				derived.Bucket("After Today", [("due_date", ">", derived.TOMORROW_START)]),
			],
		)
		problems = derived.verify(field, defaults=_defaults_for(TASK))
		self.assertTrue(
			any(p.kind == "reader-disagreement" for p in problems),
			f"the verifier missed the inclusive bound: {problems}",
		)

	def test_the_verifier_catches_overlapping_buckets(self):
		# SQL has no first-match ordering, so an overlap makes display and query name different rows.
		field = derived.DerivedField(
			doctype=TASK,
			fieldname="_probe_overlap",
			label="Probe",
			buckets=[
				derived.Bucket("Open", [("status", "not in", CLOSED)]),
				derived.Bucket("Any Status", [("status", "is", "set")]),
			],
		)
		problems = derived.verify(field, defaults=_defaults_for(TASK))
		self.assertTrue(
			any(p.kind == "overlapping-buckets" for p in problems),
			f"the verifier missed the overlap: {problems}",
		)

	def test_the_corpus_covers_each_declared_bound_and_its_neighbours(self):
		"""A boundary is only covered by sitting ON it and one unit either side of it, so this asserts the
		corpus does all three for every value the declaration names. The row exactly on the bound is what
		caught the `<=` disagreement; the neighbours are what would catch an off-by-one-second one."""
		field = _sound_field()
		snap = derived.snapshot()
		combinations, _wanted = derived._corpus(field, snap)
		self.assertEqual(set(combinations[0]), set(field.depends_on))
		due_dates = {str(c["due_date"]) for c in combinations}
		self.assertIn("None", due_dates, "the corpus never tries an empty due date")
		for token in (derived.NOW, derived.TOMORROW_START):
			bound = get_datetime(snap[token])
			for label, moment in (
				("on", bound),
				("one second before", add_to_date(bound, seconds=-1)),
				("one second after", add_to_date(bound, seconds=1)),
			):
				self.assertIn(str(moment), due_dates, f"the corpus never sits {label} {token}")

	def test_a_capped_corpus_is_reported_never_silent(self):
		original = derived.CORPUS_LIMIT
		derived.CORPUS_LIMIT = 2
		try:
			problems = derived.verify(_sound_field(), defaults=_defaults_for(TASK))
		finally:
			derived.CORPUS_LIMIT = original
		self.assertTrue(any(p.kind == "corpus-capped" for p in problems), f"cap unreported: {problems}")

	def test_verification_leaves_no_rows_behind(self):
		before = frappe.db.count(TASK)
		derived.verify(_sound_field(), defaults=_defaults_for(TASK))
		self.assertEqual(frappe.db.count(TASK), before)


class TestEveryDeclaredField(FrappeTestCase):
	"""The sweep that makes this generic: a new derived field is proven here, not in a test someone
	remembers to write."""

	def test_every_registered_derived_field_verifies(self):
		for field in derived.registered():
			with self.subTest(f"{field.doctype}.{field.fieldname}"):
				self.assertEqual(derived.verify(field, defaults=_defaults_for(field.doctype)), [])


class TestDerivedFieldDeclaration(FrappeTestCase):
	"""Shape checks — the things knowable without touching data."""

	def test_depends_on_is_derived_from_the_buckets_not_declared(self):
		# Declaring it separately is a second brain that drifts the moment a bucket gains a term.
		self.assertEqual(_sound_field().depends_on, ("due_date", "status"))

	def test_options_are_the_bucket_values_in_declaration_order(self):
		self.assertEqual(
			_sound_field().options, ("Overdue", "Due Today", "Upcoming", "No Due Date", "History")
		)

	def test_the_first_matching_bucket_wins(self):
		# Ordering is the contract, and only an overlapping declaration can observe it.
		field = derived.DerivedField(
			doctype=TASK,
			fieldname="_probe_order",
			label="Probe",
			buckets=[
				derived.Bucket("Open", [("status", "not in", CLOSED)]),
				derived.Bucket("Any Status", [("status", "is", "set")]),
			],
		)
		self.assertEqual(derived.value_of(field, frappe._dict({"status": "Todo"})), "Open")
		self.assertEqual(derived.value_of(field, frappe._dict({"status": "Done"})), "Any Status")

	def test_a_malformed_declaration_is_refused_at_declaration_time(self):
		cases = {
			"no buckets": [],
			"duplicate values": [
				derived.Bucket("Same", [("status", "in", CLOSED)]),
				derived.Bucket("Same", [("status", "not in", CLOSED)]),
			],
			"empty bucket": [derived.Bucket("Empty", [])],
			"malformed term": [derived.Bucket("Bad", [("status", "in")])],
		}
		for label, buckets in cases.items():
			with self.subTest(label), self.assertRaises(derived.DerivedFieldError):
				derived.register(
					derived.DerivedField(doctype=TASK, fieldname="_probe_bad", label="P", buckets=buckets)
				)

	def test_a_bucket_may_not_filter_on_the_derived_field_itself(self):
		with self.assertRaises(derived.DerivedFieldError):
			derived.register(
				derived.DerivedField(
					doctype=TASK,
					fieldname="_probe_self",
					label="P",
					buckets=[derived.Bucket("A", [("_probe_self", "=", "A")])],
				)
			)

	def test_a_fieldname_that_shadows_a_real_column_is_refused(self):
		# Shadowing a real column would make the cell and the column disagree silently.
		derived.register(
			derived.DerivedField(
				doctype=TASK,
				fieldname="status",
				label="P",
				buckets=[derived.Bucket("A", [("due_date", "is", "set")])],
			)
		)
		try:
			with self.assertRaises(derived.DerivedFieldError):
				derived.for_doctype(TASK)
		finally:
			derived._REGISTRY.get(TASK, {}).pop("status", None)
			derived._validated().discard(TASK)

	def test_a_doctype_that_declares_nothing_gets_nothing(self):
		# The inertness guarantee: until something opts in, this layer is not in the answer at all.
		self.assertEqual(derived.for_doctype("CRM Call Log"), ())
		self.assertIsNone(derived.get("CRM Call Log", "anything"))

	def test_an_unknown_value_raises_instead_of_widening_the_list(self):
		# An empty filter list would quietly return EVERY row, the worst possible answer to a bad chip.
		with self.assertRaises(derived.DerivedFieldError):
			derived.predicate(_sound_field(), "Nonsense")

	def test_one_snapshot_serves_the_filter_and_the_cell(self):
		# Two reads of the clock in one response could file a row under one bucket and show another.
		field = _sound_field()
		snap = derived.snapshot()
		terms = derived.predicate(field, "Overdue", snap)
		row = frappe._dict({"status": "Todo", "due_date": snap[derived.NOW]})
		self.assertEqual(next(t[3] for t in terms if t[2] == "<"), snap[derived.NOW])
		self.assertEqual(derived.value_of(field, row, snap), "Due Today")

	def test_the_standard_datetime_columns_have_a_fieldtype(self):
		# `creation` and `modified` are real columns `meta.get_field` does not answer for.
		self.assertEqual(derived.fieldtype_of(TASK, "creation"), "Datetime")
		self.assertEqual(derived.fieldtype_of(TASK, "due_date"), "Datetime")
		self.assertIsNone(derived.fieldtype_of(TASK, "not_a_column"))
