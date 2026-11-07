"""Existing analytics rows take the convention's shape (docs/conventions/backend/analytics-facts.md): a Call Fact names its call
log and carries its grain key, built by the job's own helper; a Call Summary row with no snapshot is derived, so it goes and the
next run rebuilds today's. Idempotent. Rows of the newer facts never shipped in the old shape, so they need nothing."""

import frappe

from tatva_connect.tatva_analytics import facts


def execute():
	if frappe.db.table_exists("Tatva Call Fact"):
		fact = frappe.qb.DocType("Tatva Call Fact")
		frappe.qb.update(fact).set(fact.call_log, fact.name).where(fact.call_log.isnull()).run()
		# sqli-ok: the grain key is the job's own constant SQL over a column name, no caller input.
		frappe.db.sql(f"UPDATE `tabTatva Call Fact` SET grain_key = {facts.grain_key('call_log')} WHERE grain_key IS NULL")
	if frappe.db.table_exists("Tatva Call Summary"):
		frappe.db.delete("Tatva Call Summary", {"snapshot": ("is", "not set")})
