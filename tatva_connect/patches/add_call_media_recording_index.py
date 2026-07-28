# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Index CRM Call Media (recording_state, recording_next_attempt_at) — the media sweep's one question.

*"Which calls are still owed a recording, and which of those may be retried now?"* is asked on every
sweep. The pair is equality-then-range — the sweep matches `recording_state` exactly and compares
`recording_next_attempt_at` against now — which is the order that lets ONE index answer the whole
question, including its LIMIT. Composite, so it cannot be declared in the doctype JSON; this and
`schema_setup` are its only two doors.

`frappe.db.add_index` and NOT `patches/_schema.ddl`: that door exists because a raw ALTER leaves frappe's
cached COLUMN list stale (`database.py:1334`), and an index changes no columns, so there is nothing to
invalidate. The DDL lock forbids `sql_ddl`/`rename_column`/`rename_field` and not this, and every index
patch in this app already uses `add_index`.
"""
import frappe

from tatva_connect.storage import call_media


def execute():
	if not frappe.db.table_exists(call_media.MEDIA_DT):
		return  # the doctype syncs later in this same migrate; schema_setup lands the index after it
	if frappe.db.has_index(f"tab{call_media.MEDIA_DT}", call_media.RECORDING_INDEX):
		return
	try:
		frappe.db.add_index(call_media.MEDIA_DT, call_media.RECORDING_INDEX_FIELDS, call_media.RECORDING_INDEX)
	except Exception:
		frappe.log_error(title="CRM Call Media recording index failed", message=frappe.get_traceback())
