# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""A Live import's three ticks (emails, in-app, push) become one Don't Notify Assignees: frappe's `assign_to` has one switch, so a job has one. A row is quiet when it ticked any of the three, else not; then the three columns go. Idempotent. No schema_setup twin: a fresh site never has the old columns."""
import frappe
from frappe.query_builder import Case, Criterion

from tatva_connect.patches import _schema

_OLD = ("skip_email", "skip_in_app", "skip_push")
_NEW = "skip_notifications"


def execute():
	for doctype in ("CRM Lead Import", "CRM Bulk Job"):
		old = [field for field in _OLD if frappe.db.has_column(doctype, field)]
		if not old:
			continue
		table = frappe.qb.DocType(doctype)
		# Set on every row, not only the ticked ones: model sync just added the new column at its doctype default, which is not the row's choice.
		frappe.qb.update(table).set(table[_NEW], Case().when(Criterion.any([table[f] == 1 for f in old]), 1).else_(0)).run()
		# One statement, so a migrate that dies here never leaves some old ticks to be re-read on the rerun.
		_schema.ddl(f"ALTER TABLE `tab{doctype}` " + ", ".join(f"DROP COLUMN `{field}`" for field in old), f"tab{doctype}")
		frappe.clear_cache(doctype=doctype)
