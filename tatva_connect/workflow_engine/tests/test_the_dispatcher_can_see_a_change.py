# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""`changed to` needs a before-value, and nothing proved one was ever produced.

Measured 2026-08-17: `watchable_fields("CRM Lead")` returned 0 against 366 catalog rows. Every catalog seed
writes `can_watch = 0`, nothing ever ticked one, and the doctype default is 0 — so `diff_watched_fields`
returned `{}` on every lead save, no `<field>__before` was ever written, and `changed to` /
`changed from…to` matched nothing on any lead, ever. Every suite was green throughout.

The gap was that the operators were tested against a HAND-BUILT diff. A test that supplies the before-value
it is about to assert on cannot notice that nothing in production supplies one. These drive the real path —
`diff_watched_fields` reads the catalog, `context_for` writes the pair, `rules` compares it — so the suite
goes red when the catalog cannot answer.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import context as ctx_build
from tatva_connect.automation import fields, rules
from tatva_connect.workflow_engine import refs

_LEAD_DT = "CRM Lead"
_FIELD = "status"


class TestTheDispatcherCanSeeAChange(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		cls.lead = frappe.db.get_value(_LEAD_DT, {}, "name")

	def setUp(self):
		if not self.lead:
			self.skipTest("no CRM Lead on this bench to diff")
		frappe.flags.pop("_watchable_fields_cache", None)

	def _changed(self, fieldname, to):
		"""One save's worth of diff, through the real reader — never a hand-built pair."""
		doc = frappe.get_doc(_LEAD_DT, self.lead)
		doc._doc_before_save = frappe.get_doc(_LEAD_DT, self.lead)
		doc.set(fieldname, to)
		return doc, ctx_build.diff_watched_fields(doc)

	def test_at_least_one_lead_field_is_watchable(self):
		"""The bare fact that was false site-wide. Without it every assertion below is vacuous."""
		self.assertTrue(
			fields.watchable_fields(_LEAD_DT),
			"no CRM Lead field is watchable, so `changed to` can never match — see "
			"db-seeds/1-before-load/2026-08-17-lead-catalog-watchable.sql",
		)

	def test_the_field_the_business_triggers_on_is_watchable(self):
		"""`status` is what a stage-change workflow watches. A catalog that ticks everything BUT this one
		would pass the test above and still leave the business's own trigger dead."""
		self.assertTrue(fields.is_watchable(_LEAD_DT, _FIELD))

	def test_a_real_save_produces_a_before_value(self):
		"""The whole chain: the catalog decides what is diffed, the diff produces the pair."""
		_doc, changed = self._changed(_FIELD, "__probe__")
		self.assertIn(_FIELD, changed, "the diff saw no change on a field that changed")
		self.assertEqual(changed[_FIELD][1], "__probe__")

	def test_the_before_value_reaches_the_predicate_context(self):
		"""`context_for` is what a Trigger predicate reads. The suffix goes on the FIELD, never the source."""
		doc, changed = self._changed(_FIELD, "__probe__")
		ctx = ctx_build.context_for(doc, changed, lead=doc)
		ref = refs.of_record(_LEAD_DT, _FIELD)
		self.assertEqual(ctx.get(refs.before(ref)), changed[_FIELD][0])
		self.assertEqual(ctx.get(ref), "__probe__")

	def test_changed_to_actually_matches(self):
		"""The operator itself, judged by the ONE evaluator the Trigger uses — not by re-implementing it."""
		doc, changed = self._changed(_FIELD, "__probe__")
		ctx = ctx_build.context_for(doc, changed, lead=doc)
		predicate = {"type": "all", "children": [
			{"type": "rule", "field": refs.of_record(_LEAD_DT, _FIELD), "operator": "changed to",
			 "value": "__probe__"},
		]}
		self.assertTrue(rules.predicate_match(predicate, ctx))

	def test_an_unchanged_field_does_not_match(self):
		"""The other direction: an operator that matched everything would pass the test above too."""
		doc, changed = self._changed(_FIELD, "__probe__")
		ctx = ctx_build.context_for(doc, changed, lead=doc)
		predicate = {"type": "all", "children": [
			{"type": "rule", "field": refs.of_record(_LEAD_DT, _FIELD), "operator": "changed to",
			 "value": "__never__"},
		]}
		self.assertFalse(rules.predicate_match(predicate, ctx))
