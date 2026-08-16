"""Composite index (reference_docname, creation) on Integration Request — the pair the Activity rail reads
it by, now that the Call API node's record is a rail subject.

The table is frappe's SHARED outbound log: whatsapp, voice and telephony each write a row per call they
make, so it is one of the busiest tables on the site and the rail's read is a needle in it. It carries an
index on `creation`, and `tc_service_status (integration_request_service, status)` from
`add_integration_request_index` — neither can serve "this lead's rows, newest first", so without this the
rail leg scans the whole log on every page view.

`reference_docname` leads and not `integration_request_service`: the lead is the selective column (one
patient's handful of rows out of every integration request the site ever made), while the service has five
distinct values and would put the scan back. `creation` rides in the leaf so the rail's ORDER BY is served
by the index instead of a filesort. `reference_doctype` is deliberately NOT in it — a docname is unique
across doctypes here in practice, and leading with a two-value column would waste the first level.

Doctype JSON cannot express a composite index, and this is a FRAPPE-owned doctype, so a JSON edit is not
available at all. Idempotent (has_index guard). install-app baselines patches.txt without running it, so
this also runs on after_migrate via schema_setup (mirrors add_step_log_journey_index)."""

import frappe

_TABLE = "tabIntegration Request"
_INDEXES = (("ix_ir_reference_creation", ("reference_docname", "creation")),)


def execute():
	if not frappe.db.table_exists("Integration Request"):
		return
	for name, columns in _INDEXES:
		if frappe.db.has_index(_TABLE, name):
			continue
		try:
			frappe.db.add_index("Integration Request", list(columns), name)
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"activity rail: index {name} failed")
