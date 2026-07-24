"""Index the webhook log on the two columns every operator query filters by.

`Integration Request` belongs to frappe, which declares no index on it and retains it for 90 days.
The DLQ replay and every Desk filter select on (service, status), so without this they scan a table
that grows by thousands of rows a day on a busy provider account.

Idempotent, and run from both `patches.txt` (existing sites) and `schema_setup` (fresh installs,
where `install-app` baselines patches.txt without running it).
"""
import frappe

DOCTYPE = "Integration Request"
INDEX = "tc_service_status"
COLUMNS = ("integration_request_service", "status")


def execute():
	if not frappe.db.table_exists(DOCTYPE):
		return
	if frappe.db.has_index(f"tab{DOCTYPE}", INDEX):
		return
	try:
		frappe.db.add_index(DOCTYPE, list(COLUMNS), index_name=INDEX)
	except Exception:
		# The only index step that was not isolated. A missing index is a slow query, never a reason to abort a migrate — same stance as its siblings.
		frappe.log_error(title="Integration Request index failed", message=frappe.get_traceback())
