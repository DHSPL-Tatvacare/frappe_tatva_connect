"""One-time backfill: canonicalise empty CRM Lead routing fields ''->NULL.

The dedup anchor keys on {mobile_no, custom_vertical, custom_group}. get_value with a
None routing value builds `IS NULL`, which MISSES leads stored with '' (common from the
intake form / imports) -> a duplicate lead slips through. The before_validate hook
(canonicalize_routing_fields) keeps NEW writes NULL; this sweeps existing rows so the
whole table is consistent. Idempotent (re-running is a no-op once all rows are NULL).
"""
import frappe

FIELDS = ["custom_vertical", "custom_group", "custom_current_program"]


def execute():
	for f in FIELDS:
		if frappe.db.has_column("CRM Lead", f):
			frappe.db.sql(
				"UPDATE `tabCRM Lead` SET `{0}` = NULL WHERE `{0}` = ''".format(f)
			)
	frappe.db.commit()
