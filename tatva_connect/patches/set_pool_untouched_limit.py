"""Every Credit Weighted pool gets the limit of 3 untouched leads per rep (DA55); a fixture default never fills rows that already exist. Idempotent: only blank limits are set."""
import frappe

from tatva_connect.lead.routing import DEFAULT_MAX_UNTOUCHED


def execute():
	if not frappe.db.has_column("Assignment Rule", "max_untouched"):
		return
	for name in frappe.get_all("Assignment Rule", {"rule": "Credit Weighted", "max_untouched": ["in", (None, 0)]}, pluck="name"):
		frappe.db.set_value("Assignment Rule", name, "max_untouched", DEFAULT_MAX_UNTOUCHED, update_modified=False)
