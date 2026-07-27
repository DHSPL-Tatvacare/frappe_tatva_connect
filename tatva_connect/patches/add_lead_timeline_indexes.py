"""Index the two lead-timeline reads that still scan: FCRM Note and CRM Call Log, by (reference_docname, creation).

The lead's Notes and Calls tabs both filter on `reference_docname` ALONE. Measured with EXPLAIN on a dev
site: FCRM Note is `type: ALL` — a full table scan, no index on that column at all — and CRM Call Log is
`type: index` with `possible_keys: NULL`, a full index scan, because `ix_calllog_reference` leads with
`reference_doctype` and MariaDB cannot seek a non-leading column. CRM Task is the one path that already
seeks, and it does so because `ix_refdoc_tasktype_status` leads with `reference_docname` — so this is the
app's own shape, applied to the two tables that were missed.

`creation` is the second column because these tabs are ordered newest-first and are about to be paginated:
the pair serves the filter, the sort and the LIMIT from one seek instead of sorting a scan.

ADDITIVE. `ix_calllog_reference` stays — `call_list` filters on both columns and is served by it.
"""
import frappe

INDEXES = (
	("FCRM Note", "ix_note_refdoc_creation"),
	("CRM Call Log", "ix_calllog_refdoc_creation"),
)
FIELDS = ["reference_docname", "creation"]


def execute():
	for doctype, index in INDEXES:
		if frappe.db.has_index(f"tab{doctype}", index):
			continue
		try:
			frappe.db.add_index(doctype, FIELDS, index)
		except Exception:
			frappe.log_error(
				title=f"{doctype} timeline index failed",
				message=frappe.get_traceback(),
			)
