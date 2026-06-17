"""Composite indexes on CRM API Metric (the immortal aggregate table).

Doctype JSON can't express composite indexes, so — like
recreate_whatsapp_message_id_index_composite — we build them in code. Idempotent
(SHOW INDEX guard). Runs on after_migrate via schema_setup for fresh installs, and from
patches.txt [post_model_sync] for existing DBs.

    ix_gran_bucket   (granularity, bucket_start)            -> chart range queries
    ix_gran_ep       (granularity, endpoint, bucket_start)  -> per-endpoint dashboards

The raw CRM API Request Log only needs a single index on request_time, which is
declared as `search_index` in its JSON, so Frappe builds it automatically — not here.
"""
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
		if frappe.db.sql(f"SHOW INDEX FROM `{_TABLE}` WHERE Key_name = %s", name):
			continue
		try:
			frappe.db.sql(f"ALTER TABLE `{_TABLE}` ADD INDEX `{name}` ({cols})")
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"observability: index {name} failed")
