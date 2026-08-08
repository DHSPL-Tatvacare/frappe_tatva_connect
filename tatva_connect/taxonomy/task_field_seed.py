"""The native CRM Task columns a declared activity field is PROMOTED to; without a row, a field falls to key-value."""
import frappe

# `can_set` is the ONE gate on promotion: a write is a write, whether an activity form or a workflow makes it.
# The four custom columns are the ones the task row RETAINS (D18); `description` is the Notes every LSQ form
# carries as an ordinary activity field its rules show, hide and require.
_ROWS = [
	{"fieldname": "status", "label": "Status", "can_watch": 1, "can_set": 0},
	{"fieldname": "custom_outcome", "label": "Outcome", "can_watch": 1, "can_set": 1},
	{"fieldname": "custom_followup_at", "label": "Follow-up At", "can_watch": 0, "can_set": 1},
	{"fieldname": "custom_scheduled_at", "label": "Scheduled At", "can_watch": 0, "can_set": 1},
	{"fieldname": "custom_asm", "label": "ASM", "can_watch": 0, "can_set": 1},
	{"fieldname": "description", "label": "Notes", "can_watch": 0, "can_set": 1},
]

_FLAGS = ("can_watch", "can_set")


def ensure_rows():
	"""Idempotent: asserts the flags our code routes by, and leaves an operator's label alone."""
	if not frappe.db.table_exists("CRM Task Field"):
		return  # skip-until-ready: the doctype has not synced yet; the after_migrate pass seeds the rows then
	task_meta = frappe.get_meta("CRM Task")
	for row in _ROWS:
		if not task_meta.get_field(row["fieldname"]):
			continue  # a column the fixture has not landed yet; the after_migrate pass seeds it once it has
		flags = {f: row[f] for f in _FLAGS}
		if frappe.db.exists("CRM Task Field", row["fieldname"]):
			frappe.db.set_value("CRM Task Field", row["fieldname"], flags)  # authz-ok: tier-c — after_migrate, structural flags this app owns
		else:
			frappe.get_doc({"doctype": "CRM Task Field", **row}).insert(ignore_permissions=True)  # authz-ok: tier-c — after_migrate, no session user
	frappe.db.commit()
