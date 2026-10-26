# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""HD Ticket Routing and HD Ticket Transition take `disabled`, the flag every other Helpdesk master here uses, in place of `enabled`: each row keeps its state, then `enabled` goes. Idempotent. No schema_setup twin: a fresh site never has the old column."""
import frappe
from frappe.query_builder import Case

from tatva_connect.helpdesk import ROUTING, TRANSITION
from tatva_connect.patches import _schema


def execute():
	for doctype in (ROUTING, TRANSITION):
		if not frappe.db.has_column(doctype, "enabled"):
			continue
		table = frappe.qb.DocType(doctype)
		# Set on every row, not only the switched-off ones: model sync just added `disabled` at its default, which is not the row's choice.
		frappe.qb.update(table).set(table.disabled, Case().when(table.enabled == 0, 1).else_(0)).run()
		_schema.ddl(f"ALTER TABLE `tab{doctype}` DROP COLUMN `enabled`", f"tab{doctype}")
		frappe.clear_cache(doctype=doctype)
