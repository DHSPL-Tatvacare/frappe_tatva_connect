# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""THE LOCK on W7.2's ONE SELECTOR: the row it reads must answer exactly what a hydrated lead does.

`cohort.matching_leads` used to call `frappe.get_doc` per candidate, which loads every child table to judge
a predicate that usually reads a handful of lead columns — 12 queries a lead, and the pace is on MATCHES,
so a selective predicate walked tens of thousands of rows inside one chunk. It now reads the subject's own
columns WITH the page and builds the doc in memory.

THE ORACLE IS THE WALK IT REPLACED. Every scenario below is judged twice — once through the selector, once
through `_hydrating_walk`, which is the old loop verbatim — and the two `(matched, scanned_to)` answers must
be identical, member for member and in order. So this suite goes RED the moment they disagree, whatever
moved: a predicate shape `_row_columns` should not have admitted, a context key the row cannot carry, a
page that stopped reading a column, a cursor that drifted off the scan frontier.

CHILD SECTIONS ARE THE ONE THING A ROW CANNOT ANSWER, and `test_the_gate_is_load_bearing` proves it by
writing the evasion — forcing the row path onto a section predicate — and watching it answer wrong.
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import context as ctx_build
from tatva_connect.automation import rules
from tatva_connect.workflow_engine import cohort, refs
from tatva_connect.workflow_engine.tests import fixtures as fx

_SUBJECT = "CRM Lead"
_METRICS = "custom_lead_activity_metrics"
_COUNT = "custom_total_phone_call_count"


def _rule(field, operator, value=None, **more):
	return {
		"type": "rule", "field": refs.of_record(_SUBJECT, field), "operator": operator, "value": value, **more,
	}


def _hydrating_walk(subject, config, after=None, limit=cohort._PAGE):
	"""THE ORACLE — the selector as it was, one `frappe.get_doc` per candidate.

	Kept verbatim rather than derived: it is the answer this suite defends, so it must not move when the
	thing it is checking moves. The predicate is judged by the SAME `rules.predicate_match` either way —
	the point being tested is what the context is built FROM, never who decides.
	"""
	predicate = config.get("predicate")
	fields = ctx_build.fields_for(subject)
	base = cohort._grain_filters(config)
	matched, cursor = [], after
	while len(matched) < limit:
		filters = dict(base)
		if cursor:
			filters["name"] = [">", cursor]
		rows = frappe.get_all(
			subject, filters=filters, fields=["name"], order_by="name asc", limit=cohort._PAGE,
		)
		if not rows:
			return matched, cursor
		cursor = rows[-1].name
		for row in rows:
			if predicate:
				doc = frappe.get_doc(subject, row.name)
				if not rules.predicate_match(predicate, ctx_build.context_for(doc, {}), fields):
					continue
			matched.append(row.name)
			if len(matched) == limit:
				return matched, row.name
	return matched, cursor


class TestTheSelectorAnswersOffTheRow(FrappeTestCase):
	"""One cohort of probe leads, judged every shape a predicate comes in."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.config = {**fx.GRAIN}
		cls.marker = "cohort-selector-lock"
		cls.leads = [
			fx.make_lead(custom_external_id=cls.marker, status="New"),
			fx.make_lead(custom_external_id=cls.marker, status="Qualified"),
			fx.make_lead(custom_external_id=cls.marker, status="New", **{_METRICS: [{_COUNT: 3}]}),
			fx.make_lead(custom_external_id=cls.marker, status="New", **{_METRICS: [{_COUNT: 0}]}),
			fx.make_lead(status="New"),
		]
		frappe.db.commit()
		# A lead's name is a HASH, so the last name on the grain is not the newest lead: a cursor taken before
		# seeding starts past what was just seeded, and every scenario below then compares nothing to nothing.
		first = min(lead.name for lead in cls.leads)
		prior = frappe.get_all(_SUBJECT, filters={**cohort._grain_filters(cls.config), "name": ["<", first]},
		                       order_by="name desc", limit=1)
		cls.after = prior[0].name if prior else None

	@classmethod
	def tearDownClass(cls):
		for lead in cls.leads:
			frappe.delete_doc(_SUBJECT, lead.name, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def _shapes(self):
		"""Every predicate shape the selector must answer the same way — flat, grouped, typed, and the
		child-section paths that are the reason a second path exists at all."""
		return {
			"no predicate": None,
			"flat is": _rule("status", "is", "New"),
			"flat is not": _rule("status", "is not", "New"),
			"selective is": _rule("status", "is", "Qualified"),
			"nested all": {"type": "all", "children": [
				_rule("status", "is", "New"), _rule("custom_external_id", "is", self.marker)]},
			"nested any": {"type": "any", "children": [
				_rule("status", "is", "Qualified"), _rule("status", "is", "New")]},
			"not": {"type": "not", "children": [_rule("status", "is", "New")]},
			"membership": _rule("status", "is one of", "New, Qualified"),
			"date comparison": _rule("creation", "greater than", "2000-01-01 00:00:00"),
			"date between": _rule("creation", "is between", "2100-01-01 00:00:00",
			                      from_value="2000-01-01 00:00:00"),
			"is set": _rule("custom_external_id", "is set"),
			"is not set": _rule("custom_external_id", "is not set"),
			"contains": _rule("lead_name", "contains", "WF"),
			"changed to": _rule("status", "changed to", "New"),
			"virtual field": _rule("custom_age", "at least", "1"),
			"child section is set": _rule(f"{_METRICS}.{_COUNT}", "is set"),
			"child section compared": _rule(f"{_METRICS}.{_COUNT}", "greater than", "0"),
			"child section inside a group": {"type": "all", "children": [
				_rule("status", "is", "New"), _rule(f"{_METRICS}.{_COUNT}", "greater than", "0")]},
		}

	def test_every_predicate_shape_selects_exactly_what_a_hydrated_lead_would(self):
		"""THE LOCK. Same members, same order, same `scanned_to` — a cohort that admits one different lead
		is a worse defect than the cost this change removed."""
		for limit in (2, 50):
			for label, predicate in self._shapes().items():
				config = {**self.config, "predicate": predicate}
				with self.subTest(shape=label, limit=limit):
					self.assertEqual(
						cohort.matching_leads(_SUBJECT, config, after=self.after, limit=limit),
						_hydrating_walk(_SUBJECT, config, after=self.after, limit=limit),
					)

	def test_the_count_the_preview_shows_is_the_cohort_the_drain_walks(self):
		"""`_count_matching` PAGES the one selector, so a cohort that spans chunks must be counted once each.

		Chunked well under a page so the walk really crosses a boundary: the number an author is armed by
		and the leads the drain starts come from the same pages, and a lead lost or repeated between them
		is the truncation `(matched, scanned_to)` exists to prevent.
		"""
		config = {**self.config, "predicate": _rule("status", "is", "New")}
		walked, cursor = [], None
		while True:
			page, cursor = cohort.matching_leads(_SUBJECT, config, after=cursor, limit=17)
			if not page:
				break
			walked += page
		self.assertEqual(len(set(walked)), len(walked), "a lead was counted twice across a chunk boundary")
		self.assertEqual(cohort._count_matching(_SUBJECT, config, cohort.PREVIEW_CAP), (len(walked), False))

	def test_an_authoring_fault_still_raises_where_it_sat(self):
		"""A predicate naming what the subject does not have is LOUD in both paths — a selector that
		quietly returned nobody is the failure `PredicateError` exists to prevent."""
		for label, predicate in {
			"unknown field": _rule("no_such_field_at_all", "is", "x"),
			"unknown operator": _rule("status", "sideways", "New"),
			"no field named": {"type": "rule", "operator": "is", "value": "New"},
			"unknown node type": {"type": "perhaps", "children": []},
		}.items():
			config = {**self.config, "predicate": predicate}
			with self.subTest(shape=label):
				self.assertRaises(rules.PredicateError, cohort.matching_leads, _SUBJECT, config,
				                  self.after, 50)

	def test_a_row_answerable_predicate_hydrates_nobody(self):
		"""THE DEFECT ITSELF, held shut: the walk costs no per-lead document load. A revert to
		`frappe.get_doc(subject, name)` here is what made one canvas click scan a whole grain."""
		config = {**self.config, "predicate": _rule("status", "is", "New")}
		loaded = []
		real = frappe.get_doc

		def watching(*args, **kwargs):
			if args and isinstance(args[0], str):
				loaded.append(args[0])
			return real(*args, **kwargs)

		with patch.object(frappe, "get_doc", watching):
			cohort.matching_leads(_SUBJECT, config, after=self.after, limit=50)
		self.assertEqual([dt for dt in loaded if dt == _SUBJECT], [])

	def test_the_gate_is_load_bearing(self):
		"""THE EVASION, written and watched to fail. A child section lives in rows the page never reads, so
		the row path answers a section predicate WRONG — which is why `_row_columns` refuses it.

		If this ever passes, the row genuinely answers sections and the refusal should be revisited; it must
		never pass by accident.
		"""
		config = {**self.config, "predicate": _rule(f"{_METRICS}.{_COUNT}", "greater than", "0")}
		truth = _hydrating_walk(_SUBJECT, config, after=self.after, limit=50)
		self.assertTrue(truth[0], "the fixture must include a lead the section predicate really selects")
		self.assertIsNone(cohort._row_columns(_SUBJECT, config["predicate"]))
		with patch.object(cohort, "_row_columns",
		                  lambda subject, predicate: frappe.get_meta(subject).get_valid_columns()):
			self.assertNotEqual(cohort.matching_leads(_SUBJECT, config, after=self.after, limit=50), truth)
