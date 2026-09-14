# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A lead catalog row is not proof a column may be WRITTEN, and the one walk behind the picker is walked once.

D1. `CRM Lead API Field` is a READ catalog, so frappe's own framework columns (`model.default_fields` +
`model.optional_fields` — `name`, `owner`, `creation`, `modified`, `_assign`) are rows in it and a criterion
may test them. They are DocFields of nothing, which is why `_update_field` refuses them off `tdoc.meta` at
EXECUTION — and `is_set_declared` said yes, so a graph naming `owner` published `{"ok": true}` and every
journey on it died on its first fire. A.21: an author error is DATA returned at publish, anchored to the
node and the field. The test derives the columns from frappe's tuples rather than typing them, because the
four that are visibly wrong are five.

D2. `_lead_rows_in_grain` is asked once per write target plus once for the criterion list — ten identical
`SELECT ... FROM tabCRM Lead API Field` per canvas load. The walk is now request-cached on the SAME
`access.request_cache` the entitlement ticks use. The grain is NOT in the key because it is not in the
answer: membership is decided by `ticked`, outside the cache — which is what the keying tests below prove.
"""
import frappe
from frappe.model import default_fields, optional_fields
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import fields
from tatva_connect.workflow_engine import graph

# A warm request still resolves link titles and meta on the first ask; the walk itself must cost nothing.
_WALK_BUDGET = 0


def _framework_columns_in_the_catalog():
	"""The catalog rows that name a column frappe manages itself — read off frappe, never typed here."""
	catalog = set(frappe.get_all("CRM Lead API Field", pluck="fieldname", distinct=True))
	return sorted(catalog & (set(default_fields) | set(optional_fields)))


def _bust():
	"""Drop this request's cached walk, so a test measures the cold build it means to measure."""
	setattr(frappe.local, fields._ROWS_CACHE, None)


def _graph(config):
	"""Trigger → the Update Field under test → End, so any problem is the one this suite asked about."""
	return [
		{"node_id": "start", "node_type": "Trigger",
		 "config": {"subject_doctype": "CRM Lead", "event": "Created"},
		 "edges": [{"from_output": "next", "to_node": "u1"}]},
		{"node_id": "u1", "node_type": "Update Field", "config": config,
		 "edges": [{"from_output": "next", "to_node": "end"}]},
		{"node_id": "end", "node_type": "Terminal", "edges": []},
	]


class TestSettableIsARealColumn(FrappeTestCase):
	"""D1 — the write gate tells the truth about a column the runtime cannot write."""

	def setUp(self):
		self.framework = _framework_columns_in_the_catalog()

	def test_the_premise_holds(self):
		"""If no framework column is catalogued the rest of this suite proves nothing."""
		self.assertTrue(self.framework, "no framework column is a catalog row — this suite is vacuous")

	def test_a_framework_column_is_not_declared_settable(self):
		"""The publish-time half. This is the assertion that was False before the fix."""
		for name in self.framework:
			with self.subTest(field=name):
				self.assertFalse(fields.is_set_declared("CRM Lead", name),
				                 f"publish would accept a node setting {name}, which execution refuses")

	def test_a_framework_column_is_not_settable_at_a_data_grain(self):
		"""The runtime half, at every grain a contract declares — no grain may reach these columns."""
		for g in frappe.get_all("CRM Lead API Mapping", filters={"is_internal": 1},
		                        fields=["vertical", "crm_group", "program"]):
			axes = (g.vertical or "", g.crm_group or "", g.program or "")
			for name in self.framework:
				with self.subTest(field=name, grain=axes):
					self.assertFalse(fields.is_settable("CRM Lead", name, axes))

	def test_a_framework_column_is_still_readable(self):
		"""The narrow half of the fix: these columns stay TESTABLE by a criterion. A gate that dropped them
		from the read list would pass every assertion above and silently shrink the author's vocabulary."""
		readable = {r.fieldname for r in fields.readable_rows_in_rule_grain("CRM Lead", ("", "", ""))}
		self.assertTrue(set(self.framework) & readable,
		                "the fix removed framework columns from the criterion list, which reads them fine")

	def test_a_real_lead_column_is_untouched(self):
		"""The other direction, over the WHOLE catalog: only the framework columns may have changed answer.
		A predicate that also dropped `read_only` would take the Activity Metrics counters an Increment
		node writes, and every assertion above would still pass."""
		changed = []
		for row in frappe.get_all("CRM Lead API Field", fields=["fieldname", "section"]):
			sec = frappe.get_cached_doc("CRM Lead Section", row.section)
			target = sec.target_doctype if sec.child_table_field else "CRM Lead"
			if not fields.is_set_declared(target, row.fieldname):
				changed.append(row.fieldname)
		self.assertEqual(sorted(set(changed)), self.framework,
		                 "the gate refused a column that is a real DocField of the record it lands on")

	def test_the_picker_never_offers_what_the_gate_refuses(self):
		"""Offer ⊆ gate. The picker DID offer all five, so narrowing only the gate would have traded a
		dead journey for an author who picks a field publish then rejects."""
		offered = {r.fieldname for r in fields.settable_rows_in_rule_grain("CRM Lead", ("", "", ""))}
		self.assertTrue(offered, "premise: the picker offers something")
		self.assertEqual(offered & set(self.framework), set())
		for name in sorted(offered):
			with self.subTest(field=name):
				self.assertTrue(fields.is_set_declared("CRM Lead", name))

	def test_publish_refuses_the_node_and_names_the_field(self):
		"""THE lock, at the surface the defect was reported on: `graph.problems` must return an anchored
		`{node_id, field, message}` for a graph a hand-edited or imported JSON can carry."""
		for name in self.framework:
			with self.subTest(field=name):
				problems = graph.problems(_graph({
					"target_doctype": "CRM Lead",
					"updates": [{"name": name, "mode": "Literal", "value": "x"}],
				}), entry_node="start")
				anchored = [p for p in problems if p["node_id"] == "u1" and p["field"] == "updates"]
				self.assertTrue(anchored, f"publish accepted a node setting {name}: {problems}")
				self.assertIn(name, " ".join(p["message"] for p in anchored))


class TestTheCatalogIsWalkedOncePerRequest(FrappeTestCase):
	"""D2 — one walk per request, and the grain stays outside the key."""

	def test_the_walk_is_cached_for_the_rest_of_the_request(self):
		"""Ten asks, one SELECT. This counted ten before the fix — one per writable record."""
		_bust()
		fields.settable_rows_in_rule_grain("CRM Lead", ("", "", ""))
		with self.assertQueryCount(_WALK_BUDGET):
			for _ in range(10):
				fields.settable_rows_in_rule_grain("CRM Lead", ("", "", ""))

	def test_the_same_cached_walk_answers_different_predicates_differently(self):
		"""The keying proof, and the reason the key is `all`: the grain never reaches the cache. Three
		predicates, one walk, three answers — a cache keyed on the ROWS cannot leak one grain's list."""
		_bust()
		everything = fields._lead_rows_in_grain(lambda key: True)
		self.assertTrue(everything, "premise: the catalog has rows")
		one = sorted(r.field_key for r, _ in everything)[0]
		with self.assertQueryCount(_WALK_BUDGET):
			nothing = fields._lead_rows_in_grain(lambda key: False)
			just_one = fields._lead_rows_in_grain(lambda key: key == one)
		self.assertEqual(nothing, [])
		self.assertEqual([r.field_key for r, _ in just_one], [one])

	def test_two_declared_grains_still_get_different_lists(self):
		"""The same proof in the caller's own vocabulary, over the contracts this site actually declares."""
		_bust()
		answers = {}
		for g in frappe.get_all("CRM Lead API Mapping", filters={"is_internal": 1},
		                        fields=["vertical", "crm_group", "program"]):
			axes = (g.vertical or "", g.crm_group or "", g.program or "")
			answers[axes] = frozenset(r.fieldname for r in fields.settable_rows_in_rule_grain("CRM Lead", axes))
		if len(answers) < 2:
			self.skipTest("fewer than two internal contracts on this site")
		self.assertGreater(len(set(answers.values())), 1,
		                   "every grain got the same field list — the walk is being filtered inside the cache")

	def test_the_cache_does_not_outlive_the_request(self):
		"""It lives on `frappe.local`, which frappe clears per request — never on `frappe.cache`."""
		_bust()
		fields._lead_rows_in_grain(lambda key: True)
		self.assertTrue(getattr(frappe.local, fields._ROWS_CACHE, None))
		self.assertIsNone(frappe.cache.get_value(fields._ROWS_CACHE))
