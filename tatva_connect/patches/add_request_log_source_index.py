"""Composite index (channel, source, request_time) on CRM API Request Log — Integration Last Seen reads each integration's
latest call and Integration Endpoint Breakdown reads one integration over a time range; the single request_time index serves
neither, so both scanned every retained row. Doctype JSON cannot express a composite index. Idempotent (has_index guard).
install-app baselines patches.txt without running it, so this also runs on after_migrate via schema_setup."""

import frappe

_TABLE = "tabCRM API Request Log"
_INDEXES = (("ix_channel_source_time", ("channel", "source", "request_time")),)


def execute():
	if not frappe.db.table_exists("CRM API Request Log"):
		return
	for name, columns in _INDEXES:
		if frappe.db.has_index(_TABLE, name):
			continue
		try:
			frappe.db.add_index("CRM API Request Log", list(columns), name)
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"observability: index {name} failed")
