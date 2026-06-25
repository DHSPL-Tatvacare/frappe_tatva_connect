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

from tatva_connect.whatsapp.roles import WHATSAPP_ADMIN, WHATSAPP_USER

# doctype -> {role: (read, write, create, delete)}. ONLY these roles get access; every other
# role is denied. Extend per the app-wide lock-list rollout (File, ToDo, HD Ticket, ...).
# System Manager is listed on every row because a Custom DocPerm OVERRIDES the stock DocPerm
# entirely — omit it and the stock System Manager grant would vanish.
LOCKED_MATRIX = {
	"Contact": {
		"System Manager": (1, 1, 1, 1),
		"Sales Manager": (1, 1, 1, 1),  # managers may delete (clean up duplicates/junk)
		"Sales User": (1, 1, 1, 0),  # reps cannot delete
	},
	# WhatsApp is a CAPABILITY (whatsapp/roles.py), decoupled from Sales — these upstream
	# frappe_whatsapp doctypes can't carry our perms in their own JSON, so we lock them here.
	# This RELOCATES the crm fork's add_roles() grant (which is now guarded to defer to us).
	# READ-ONLY for both roles. The WATI send fires in WhatsAppMessage.before_insert, and every
	# legit send/react path inserts with ignore_permissions AFTER validate_access + the rate-cap.
	# So no user role needs `create` — and granting it is a send-gate BYPASS: a holder could insert
	# an Outgoing row directly (frappe.client.insert) and send to any number, skipping the gate,
	# cap, and 24h window. Read is all a sender/viewer needs; sends go through the gated methods.
	"WhatsApp Message": {
		"System Manager": (1, 1, 1, 1),
		WHATSAPP_USER: (1, 0, 0, 0),
		WHATSAPP_ADMIN: (1, 0, 0, 0),
	},
	"WhatsApp Templates": {
		"System Manager": (1, 1, 1, 1),
		# Read-only mirror of WATI (templates_sync writes via low-level db_*, no user perm needed).
		WHATSAPP_USER: (1, 0, 0, 0),
		WHATSAPP_ADMIN: (1, 0, 0, 0),
	},
	# Config — Admin only (+ System Manager backstop). Users never configure accounts/settings.
	# Routing resolution reads these via frappe.get_all/get_cached_value (perm-bypassing), so a
	# User with no read here still gets the tab — verified, not assumed.
	"WhatsApp Account": {
		"System Manager": (1, 1, 1, 1),
		WHATSAPP_ADMIN: (1, 1, 1, 1),
	},
	"WhatsApp Settings": {
		"System Manager": (1, 1, 1, 1),
		WHATSAPP_ADMIN: (1, 1, 1, 1),
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


def effective_all_guest_grants(doctype):
	"""All/Guest grants that EFFECTIVELY allow write/create/delete on `doctype`.

	Resolves like Frappe does: when any Custom DocPerm exists for the doctype it OVERRIDES the
	stock DocPerm (the stock rows still physically sit in `tabDocPerm` but are ignored), so we
	read Custom DocPerm when present, else the stock DocPerm. Checking raw `tabDocPerm` alone is
	wrong — it reports a false leak for a doctype we've already locked via Custom DocPerm.
	"""
	src = "Custom DocPerm" if frappe.db.exists("Custom DocPerm", {"parent": doctype}) else "DocPerm"
	rows = frappe.get_all(
		src,
		filters={"parent": doctype, "role": ["in", ["All", "Guest"]]},
		fields=["role", "write", "create", "delete"],
	)
	return [(doctype, r.role) for r in rows if r.write or r.create or r.delete]


def assert_locked(*args, **kwargs):
	"""Fail the migrate if a locked doctype is EFFECTIVELY open to All/Guest write/create/delete
	— the Layer-4 drift guard, same idiom as automation.drift / notifications.drift."""
	bad = [grant for doctype in LOCKED_MATRIX for grant in effective_all_guest_grants(doctype)]
	if bad:
		frappe.throw(f"Permission lockdown drift — locked doctypes open to All/Guest: {bad}")
