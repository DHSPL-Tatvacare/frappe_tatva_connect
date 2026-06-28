"""Build CRM API Metric composite indexes (gran+bucket, gran+endpoint+bucket) in code — doctype JSON can't express composite indexes; idempotent (SHOW INDEX guard)."""
import frappe

_TABLE = "tabCRM API Metric"
_INDEXES = (
	("ix_gran_bucket", "`granularity`, `bucket_start`"),
	("ix_gran_ep", "`granularity`, `endpoint`, `bucket_start`"),
)


def execute():
	if not frappe.db.table_exists("CRM API Metric"):
		return
	for name, cols in _INDEXES:
		if frappe.db.has_index(_TABLE, name):
			continue
		try:
			frappe.db.add_index("CRM API Metric", [c.strip().strip("`") for c in cols.split(",")], name)
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"observability: index {name} failed")
