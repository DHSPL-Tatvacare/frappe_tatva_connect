# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A partner's lead page and a rep's own leads are read off an index in order, never sorted after a scan."""
import unittest

import frappe
from frappe.query_builder import Order
from frappe.query_builder.functions import Count

from tatva_connect.api._base import _order_by
from tatva_connect.api.partner import _lead_filters

LEAD = "CRM Lead"


def _plan(filters):
	"""EXPLAIN of the page query exactly as the API builds it: its filters, its order, one page."""
	sql = frappe.get_all(LEAD, filters=filters, pluck="name", order_by=_order_by("modified"), limit_page_length=20, run=0)
	return frappe.db.sql(f"EXPLAIN {sql}", as_dict=True)[0]  # sqli-ok: the statement is frappe's own built query


class TestLeadPagesReadOffAnIndex(unittest.TestCase):
	def _busiest(self, *columns):
		lead = frappe.qb.DocType(LEAD)
		cols = [lead[c] for c in columns]
		query = frappe.qb.from_(lead).select(*cols, Count("*").as_("n")).groupby(*cols).orderby("n", order=Order.desc).limit(1)
		for col in cols:
			query = query.where(col.isnotnull() & (col != ""))
		row = query.run(as_dict=True)
		if not row:
			self.skipTest(f"no lead on this bench sets {columns}")
		return row[0]

	def test_a_partner_page_walks_the_line_index_in_order(self):
		line = self._busiest("custom_vertical", "custom_group")
		plan = _plan(_lead_filters(frappe._dict(vertical=line.custom_vertical, crm_group=line.custom_group), {}))
		self.assertEqual(plan["key"], "ix_lead_line_modified", plan)
		self.assertNotIn("filesort", plan["Extra"] or "", plan)

	def test_a_reps_own_leads_walk_the_owner_index_in_order(self):
		owner = self._busiest("lead_owner")
		plan = _plan({"lead_owner": owner.lead_owner})
		self.assertEqual(plan["key"], "ix_lead_owner_modified", plan)
		self.assertNotIn("filesort", plan["Extra"] or "", plan)

	def test_the_indexes_the_composites_replace_are_gone(self):
		indexed = {(r.Key_name, r.Column_name) for r in frappe.db.sql("SHOW INDEX FROM `tabCRM Lead`", as_dict=True)}
		self.assertNotIn(("lead_owner_index", "lead_owner"), indexed)
		self.assertFalse([k for k, c in indexed if c == "custom_lsq_prospect_id"], "the migration-only LSQ unique is still there")
