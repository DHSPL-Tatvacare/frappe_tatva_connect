"""Seed CRM Task's native automatable columns into the `CRM Task Field` catalog — completing 'one brain per
resource' for Task (its native columns; its per-task-type declared fields live on `CRM Task Type Field`).

`status` is watchable so the task-completion rules fire on the not-Done→Done TRANSITION (`changed to Done`)
exactly once — never on a re-save of an already-Done task. Declares the end state (these rows + flags),
idempotent, safe to run twice; no-op if the flags already match.
"""
import frappe

_ROWS = [
	{"fieldname": "status", "label": "Status", "can_read": 1, "can_watch": 1, "can_set": 0},
]


def execute():
	if not frappe.db.exists("DocType", "CRM Task Field"):
		return  # doctype not synced (shouldn't happen post-model-sync) — nothing to seed.
	for row in _ROWS:
		flags = {k: row[k] for k in ("can_read", "can_watch", "can_set")}
		if frappe.db.exists("CRM Task Field", row["fieldname"]):
			frappe.db.set_value("CRM Task Field", row["fieldname"], flags)
		else:
			frappe.get_doc({"doctype": "CRM Task Field", **row}).insert()
	frappe.db.commit()
