# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A saved condition that cannot be resolved narrows to nothing — wherever it sits in its group.

`_predicate_where` answers an unresolvable leaf with `_never_matches()`, which is the fail-CLOSED half of
the rule `_apply_filters` states: a saved predicate naming a field outside the reader's grain must select
nothing rather than disappear (dropped, it WIDENS the view).

It returned that answer as a `PseudoColumn`, which is a bare pypika Term with no `&`, `|` or `~`. The
group fold takes `parts[0]` and ANDs the rest onto it, so the constant survived only while it was NOT the
first part. First in its group it raised `TypeError: unsupported operand type(s) for &`, and the request
died with a 500 — in exactly the case the constant exists for.

The discriminator needs no fixture data: a field_key that is not a catalog row cannot resolve on any site,
and the group fold is pure term algebra.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.smartview.test_a_condition_that_cannot_resolve_fails_closed
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.smartview import query
from tatva_connect.smartview.catalog import LEAD_DOCTYPE, _catalog_fields

# A key no catalog can carry, so the leaf naming it is unresolvable on every site.
MISSING = "lead:zz_not_a_catalog_field"


class TestAConditionThatCannotResolveFailsClosed(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.cat = _catalog_fields("Lead", None, [("", "", "")], frappe.get_roles())
		table = frappe.qb.DocType(LEAD_DOCTYPE)
		_apply, _field_terms, cls.terms = query._joins(set(cls.cat), cls.cat, table, LEAD_DOCTYPE)
		cls.resolvable = next(
			(k for k in sorted(cls.cat) if cls.cat[k].filterable and k in cls.terms), None
		)

	def _leaf(self, key):
		return {"field": key, "operator": "=", "value": "zz"}

	def _group(self, op, *keys):
		return query._predicate_where(
			{"op": op, "conditions": [self._leaf(k) for k in keys]}, self.cat, self.terms
		)

	def test_an_unresolvable_leaf_folds_where_it_is_written_first(self):
		"""THE defect: the fail-closed constant was the one part the group fold could not start from."""
		self.assertIsNotNone(self.resolvable, "the catalog must offer one filterable field, or this proves nothing")
		crit = self._group("and", MISSING, self.resolvable)
		self.assertIn("1=0", crit.get_sql(), "the unresolvable half must still narrow to nothing")

	def test_it_folds_the_same_written_last(self):
		"""The position that always worked, held so the fix cannot trade one order for the other."""
		crit = self._group("and", self.resolvable, MISSING)
		self.assertIn("1=0", crit.get_sql())

	def test_it_folds_into_an_or_group_and_a_none_of_group(self):
		"""`or` and `not` fold with `|` and `~`, which a bare Term has no more than it has `&`."""
		self.assertIn("1=0", self._group("or", MISSING, self.resolvable).get_sql())
		self.assertIn("1=0", self._group("not", MISSING, self.resolvable).get_sql())

	def test_it_selects_nothing(self):
		"""Fail-closed is the POINT: the constant has to be false against the real driving table."""
		table = frappe.qb.DocType(LEAD_DOCTYPE)
		rows = (
			frappe.qb.from_(table).select(table.name).where(query._never_matches()).limit(1).run()
		)
		self.assertFalse(rows, "a condition that cannot resolve must select no rows")
