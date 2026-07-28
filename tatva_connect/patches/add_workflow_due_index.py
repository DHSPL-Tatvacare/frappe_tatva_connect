# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Index CRM Workflow (trigger_mode, trigger_next_run_at) — the cohort drain's one question.

*"Which workflows are due?"* is asked on every sweep, and without this it is a full scan of every
workflow on the site. The pair is equality-then-range — the drain matches `trigger_mode` exactly and
compares `trigger_next_run_at` against now — which is the order that lets ONE index answer the whole
question. Composite, so it cannot be declared in the doctype JSON; this and `schema_setup` are its only
two doors.

`frappe.db.add_index` and NOT `patches/_schema.ddl`: `_schema` exists because a raw ALTER leaves frappe's
cached COLUMN list stale (`database.py:1334`), and an index changes no columns, so there is nothing to
invalidate. The DDL lock forbids `sql_ddl`/`rename_column`/`rename_field` and not this
(`test_patch_ddl_lock.py:17`), and the app's two existing index patches — `add_call_log_reference_index`,
`add_lead_dedup_unique_index` — already use `add_index`. Same door as its neighbours.
"""
import frappe

from tatva_connect.workflow_engine import cohort


def execute():
	if frappe.db.has_index("tabCRM Workflow", cohort.DUE_INDEX):
		return
	try:
		frappe.db.add_index("CRM Workflow", cohort.DUE_FIELDS, cohort.DUE_INDEX)
	except Exception:
		frappe.log_error(title="CRM Workflow due index failed", message=frappe.get_traceback())
