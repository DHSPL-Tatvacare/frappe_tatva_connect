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
from frappe import _
from frappe.permissions import reset_perms

from tatva_connect.whatsapp.roles import WHATSAPP_ADMIN, WHATSAPP_USER

# doctype -> {role: (read, write, create, delete[, if_owner])}. ONLY these roles get access; every
# other role is denied. The matrix is composed from per-app sections below (one auditable surface —
# constitution A.8/S.6: extend the ONE lock, never a parallel per-app idiom). System Manager is
# listed on every row because a Custom DocPerm OVERRIDES the stock DocPerm entirely — omit it and the
# stock System Manager grant would vanish. A 5-tuple sets if_owner (own-records-only, policy §5 rule 2).

# --- CRM / core (frappe) -----------------------------------------------------------------------
_CRM_CORE = {
	"Contact": {
		"System Manager": (1, 1, 1, 1),
		"Sales Manager": (1, 1, 1, 1),  # managers may delete (clean up duplicates/junk)
		"Sales User": (1, 1, 1, 0),  # reps cannot delete
	},
	# Comment (VAPT P1 IDOR). Stock grants write to System Manager + Website Manager only (no `All`).
	# CREATION goes through frappe_add_comment (ignore_permissions); the CRM SPA EDITS via
	# frappe.client.set_value and DELETES via frappe.client.delete (CommentArea.vue) — both run through
	# the engine. Without an owner scope one user could rewrite ANOTHER's comment (the P1). The universal
	# rule is "you may edit/delete only your OWN comment": a single `All` row, if_owner=1, write+delete
	# only. read/create stay 0 (reads go through get_activities' ignore_permissions path; creation through
	# frappe_add_comment). This closes the cross-user IDOR for EVERY commenting surface at once — CRM
	# leads, HD tickets, any future one — with nothing to maintain (policy §5 rule 2, same shape as ToDo).
	"Comment": {
		"System Manager": (1, 1, 1, 1),
		"Website Manager": (1, 1, 1, 1),  # preserve the stock grant (Custom DocPerm overrides stock)
		"All": (0, 1, 0, 1, 1),  # anyone may edit/delete ONLY their own comment (if_owner)
	},
}

# --- Helpdesk (agent-only internal; NO customer portal — product-owner decision 2026-07-09) --------
# Stock opens HD Ticket read/create to `All` (the customer-portal grant) and the KB (HD Article/
# Category) read to All+Guest. With no portal, drop `All`/`Guest` and lock to the agent roles. This
# alone fixes get_list_data (standard get_list respects perms), get_ticket_contact/get_ticket_activities
# (both has_permission("HD Ticket","read",…,throw=True) — now denies non-agents) and `new` (.insert()
# create-check). Read-only reference config (Status/Type/Priority/Template/Form Script) is left at
# stock All-READ — policy §5 rule 1 reference data the agent UI needs; not a VAPT finding.
_HELPDESK = {
	"HD Ticket": {
		"System Manager": (1, 1, 1, 1),
		"Agent": (1, 1, 1, 1),
		"Agent Manager": (1, 1, 1, 1),
	},
	"HD Article": {
		"System Manager": (1, 1, 1, 1),
		"Agent": (1, 1, 1, 1),
		"Agent Manager": (1, 1, 1, 1),
	},
	"HD Article Category": {
		"System Manager": (1, 1, 1, 1),
		"Agent": (1, 1, 1, 1),
		"Agent Manager": (1, 1, 1, 1),
	},
	"HD Article Feedback": {  # stock: All (1,1,1,1,if_owner) — a portal rating; internal-only -> agents
		"System Manager": (1, 1, 1, 1),
		"Agent": (1, 1, 1, 1),
		"Agent Manager": (1, 1, 1, 1),
	},
	"HD View": {  # stock: Guest (1,1,1,1,if_owner) — no guest role internally
		"System Manager": (1, 1, 1, 1),
		"Agent": (1, 1, 1, 1),
		"Agent Manager": (1, 1, 1, 1),
	},
}

# --- WhatsApp (capability; whatsapp/roles.py) ---------------------------------------------------
# Upstream frappe_whatsapp doctypes can't carry our perms in their own JSON, so we lock them here.
# This RELOCATES the crm fork's add_roles() grant (now guarded to defer to us). READ-ONLY for both
# roles: the WATI send fires in WhatsAppMessage.before_insert, and every legit send/react path inserts
# with ignore_permissions AFTER validate_access + the rate-cap. So no user role needs `create` — and
# granting it is a send-gate BYPASS: a holder could insert an Outgoing row directly (frappe.client.
# insert) and send to any number, skipping the gate, cap, and 24h window.
_WHATSAPP = {
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

LOCKED_MATRIX = {**_CRM_CORE, **_HELPDESK, **_WHATSAPP}


def apply(*_args, **_kwargs):
	"""Rebuild each locked doctype's permission matrix to exactly LOCKED_MATRIX (after_migrate)."""
	for doctype, roles in LOCKED_MATRIX.items():
		if not frappe.db.exists("DocType", doctype):
			continue
		reset_perms(doctype)  # drop any prior custom perms -> deterministic rebuild
		for role, perms in roles.items():
			r, w, c, d = perms[:4]
			if_owner = perms[4] if len(perms) > 4 else 0  # optional 5th element (own-records-only)
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
					"if_owner": if_owner,
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
		fields=["role", "write", "create", "delete", "if_owner"],
	)
	# An if_owner=1 grant is own-records-only — the sanctioned pattern (policy §5 rule 2, e.g. ToDo,
	# Comment). Only a NON-if_owner All/Guest write/create/delete opens every row -> that is the drift.
	return [(doctype, r.role) for r in rows if (r.write or r.create or r.delete) and not r.if_owner]


def assert_locked(*_args, **_kwargs):
	"""Fail the migrate if a locked doctype is EFFECTIVELY open to All/Guest write/create/delete
	— the Layer-4 drift guard, same idiom as automation.drift / notifications.drift."""
	bad = [grant for doctype in LOCKED_MATRIX for grant in effective_all_guest_grants(doctype)]
	if bad:
		frappe.throw(_("Permission lockdown drift — locked doctypes open to All/Guest: {0}").format(bad))
