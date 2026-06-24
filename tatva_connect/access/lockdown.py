# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Lock the stock-open doctype permission matrix on shared/core doctypes (the Layer-1 fix).

Standard doctypes (custom=0, e.g. Contact) can't carry our perms in their own JSON, so Frappe
overrides them via `Custom DocPerm` — and when ANY Custom DocPerm exists for a doctype, the
stock DocPerm is ignored entirely. We REBUILD that matrix to exactly the roles in LOCKED_MATRIX;
every other role — including the `All` role that every login holds, plus unused ERPNext roles —
is therefore denied. Fail-closed.

Runs on after_migrate, idempotently (reset then rebuild), for the SAME reason as schema_setup:
install-app baselines patches.txt WITHOUT running it, so a fresh DB must get the lock here, not
from a patch. Custom DocPerm is hash-named, so a hand-authored fixture is fragile and only upserts
listed rows; the idempotent reset+rebuild is the clean, deterministic expression of "lock to
EXACTLY this, drop anything that drifted in".

Row-scope (which records within a doctype a role may see) is a SEPARATE layer — User Permission +
the visibility brain — not this module. This module is purely the doctype-level gate.
"""
import frappe
from frappe.permissions import reset_perms

# doctype -> {role: (read, write, create, delete)}. ONLY these roles get access; every other
# role is denied. Extend per the app-wide lock-list rollout (File, ToDo, HD Ticket, ...).
LOCKED_MATRIX = {
	"Contact": {
		"System Manager": (1, 1, 1, 1),
		"Sales Manager": (1, 1, 1, 1),  # managers may delete (clean up duplicates/junk)
		"Sales User": (1, 1, 1, 0),  # reps cannot delete
	},
}


def apply(*args, **kwargs):
	"""Rebuild each locked doctype's permission matrix to exactly LOCKED_MATRIX (after_migrate)."""
	for doctype, roles in LOCKED_MATRIX.items():
		if not frappe.db.exists("DocType", doctype):
			continue
		reset_perms(doctype)  # drop any prior custom perms -> deterministic rebuild
		for role, (r, w, c, d) in roles.items():
			frappe.get_doc(
				{
					"doctype": "Custom DocPerm",
					"parent": doctype,
					"parenttype": "DocType",
					"parentfield": "permissions",
					"role": role,
					"permlevel": 0,
					"read": r,
					"write": w,
					"create": c,
					"delete": d,
				}
			).insert(ignore_permissions=True)
	frappe.clear_cache()


def assert_locked(*args, **kwargs):
	"""Fail the migrate if a locked doctype is open to All/Guest write/create/delete — the
	Layer-4 drift guard, same idiom as automation.drift / notifications.drift assert_registered."""
	locked = list(LOCKED_MATRIX)
	bad = frappe.db.sql(
		"""
		select parent, role from `tabDocPerm`
		  where parent in %(d)s and role in ('All','Guest') and (`write`=1 or `create`=1 or `delete`=1)
		union
		select parent, role from `tabCustom DocPerm`
		  where parent in %(d)s and role in ('All','Guest') and (`write`=1 or `create`=1 or `delete`=1)
		""",
		{"d": locked},
	)
	if bad:
		frappe.throw(f"Permission lockdown drift — locked doctypes open to All/Guest: {bad}")
