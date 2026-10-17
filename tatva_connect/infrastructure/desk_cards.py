# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""The Infrastructure desk's number cards that no doctype holds."""
import frappe

from tatva_connect.observability.jobs_health import _card
from tatva_connect.search.index import CRMLeadSearch


@frappe.whitelist()
def card_records_indexed(filters=None) -> dict:
	"""Rows in the spotlight search index; 0 while search is off or the index is not built."""
	frappe.has_permission("CRM Search Alias", "read", throw=True)
	engine = CRMLeadSearch()
	if not (engine.is_search_enabled() and engine.index_exists()):
		return _card(0)
	return _card(engine.sql("SELECT COUNT(*) FROM search_fts", read_only=True)[0][0])
