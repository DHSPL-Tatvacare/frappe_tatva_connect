# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Lock the stock-open doctype permission matrix on shared/core doctypes (the Layer-1 fix).

Standard doctypes (custom=0, e.g. Contact) can't carry our perms in their own JSON, so Frappe
overrides them via `Custom DocPerm` — and when ANY Custom DocPerm exists for a doctype, the
stock DocPerm is ignored entirely. We REBUILD that matrix to exactly the roles the LEDGER declares;
every other role — including the `All` role that every login holds, plus unused ERPNext roles —
is therefore denied. Fail-closed.

WHAT THIS MODULE DECIDES, AND WHAT IT DOES NOT. `ledger.py` decides WHAT a doctype may be; this module
decides only HOW that becomes rows on this site, and it is the only module in the package that writes a
permission row. The set it rebuilds is `rebuild_targets()`: every doctype the ledger OPENs, plus — for
each app armed in ENFORCED_APPS — every parent doctype of that app, which resolves to DENIED unless the
ledger names it. With ENFORCED_APPS empty the behaviour is exactly what LOCKED_MATRIX gave, and
assert_ledger_parity proves it on every migrate.

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
from frappe.permissions import add_permission, reset_perms, update_permission_property

from tatva_connect.access import ledger
from tatva_connect.whatsapp.roles import WHATSAPP_ADMIN, WHATSAPP_USER

# Apps fully inverted — every parent doctype DENIED unless the ledger opens it. Armed one app at a time.
ENFORCED_APPS = ()

# FROZEN REFERENCE — apply() reads the LEDGER now; parity asserted on migrate. Edit the ledger, not this.

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
	# Transcript + recording pointer. media_for gates on the CALL LOG then reads it with db.get_value, so the modal needs no role grant here — the Sales reads fed only /api/resource and Desk, unscoped.
	"CRM Call Media": {
		"System Manager": (1, 1, 1, 1),
	},
	# Disabled — the doctype's contract is code execution in the viewer's session, and every
	# VAPT pass found a new stored-XSS vector. Nobody may create, write, delete, or read.
	# System Manager read-only stays so an existing workspace embed still renders for an admin
	# who needs to migrate it off; every other role is denied entirely.
	"Custom HTML Block": {
		"System Manager": (1, 0, 0, 0),
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
	# `about` is a Code/HTML field rendered on the ticket form; stock lets a frontline Agent author it for every colleague. Authoring moves to Agent Manager, Agent keeps read.
	"HD Ticket Template": {
		"System Manager": (1, 1, 1, 1),
		"Agent Manager": (1, 1, 1, 1),
		"Agent": (1, 0, 0, 0),
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

# --- Wiki (internal handbook, login-only; stock ships the public docs-site surface we do not have) ---
_WIKI = {
	"Wiki Feedback": {  # drops the Guest row: an anonymous caller could create AND edit ratings
		"System Manager": (1, 1, 1, 1),
		"Wiki Approver": (1, 1, 1, 1),
		"Wiki User": (
			0,
			0,
			1,
			0,
		),  # rate a page; never read or edit one. Wiki User not All — assert_locked forbids a non-if_owner All create
	},
	"Wiki Page Patch": {  # legacy contribution flow, superseded by Wiki Change Request; drops the Guest read
		"System Manager": (
			1,
			1,
			1,
			1,
			0,
			1,
		),  # 6th element = submit/cancel/amend; without it the rebuild strips them
		"Wiki Approver": (1, 1, 1, 1, 0, 1),
		"All": (1, 1, 1, 1, 1),  # own records only — stock shape, kept
	},
}

LOCKED_MATRIX = {**_CRM_CORE, **_HELPDESK, **_WHATSAPP, **_WIKI}

# Security switches another app owns and ships permissive. Pinned on after_migrate, same reason as the matrix.
APP_SECURITY_SETTINGS = {
	# Insights queries the SITE DB directly, so with this OFF every Insights user reads every `tab*`, CRM Lead included.
	"Insights Settings": {"enable_permissions": 1, "apply_user_permissions": 1},
}

# Standard Web Forms other apps ship PUBLISHED and login-free. sync_all re-imports them on every migrate AND install, so unpublishing by hand survives neither.
UNPUBLISHED_WEB_FORMS = {
	"request-data": "frappe's GDPR data-download form — no public website, and nobody monitors the doctype",
	"request-to-delete-data": "frappe's GDPR erasure form — same",
	"email-feedback": "helpdesk's ticket-rating page — there is no customer portal",
}

# doctype -> {permlevel: {role: (read, write)}}. A permlevel-1 field is INVISIBLE to a role holding no
# permlevel-1 read — so without these rows the lock is a blackout, not a lock, and that is why the grain
# fields were quietly dropped to permlevel 0 (361a71e) to get them back on screen. The grant is what makes
# the lock usable: a rep SEES the lead's product line and group, only a manager may move the lead between
# them, and neither depends on an automation switch being on.
FIELD_LEVELS = {
	"CRM Lead": {
		1: {
			"System Manager": (1, 1),
			"Sales Manager": (1, 1),
			"Sales User": (1, 0),
		}
	},
	# A permlevel-1 CHILD field resolves against the PARENT's permlevel access, so this grant governs LMS Test Case.
	"LMS Programming Exercise": {
		1: {
			"System Manager": (1, 1),
			"Moderator": (1, 1),
			"Course Creator": (1, 1),
		}
	},
	# A program's member table names every colleague and their progress; the parent's grant governs it.
	"LMS Program": {
		1: {
			"System Manager": (1, 1),
			"Moderator": (1, 1),
			"Course Creator": (1, 1),
		}
	},
	# Connection strings and service-account keys are plaintext at permlevel 0; the Password fields are already safe.
	"Insights Data Source v3": {
		1: {
			"System Manager": (1, 1),
			"Insights Admin": (1, 1),
		}
	},
	# Head HTML is written into every wiki page unescaped — a script there runs in all 225 users' browsers.
	"Wiki Settings": {
		1: {
			"System Manager": (1, 1),
		}
	},
	# custom_script (Javascript) and custom_component (HTML) render to every learner in the batch.
	"LMS Batch": {
		1: {
			"System Manager": (1, 1),
		}
	},
	# File's computed columns (below). READ is granted to All deliberately — a blackout would blank file_url on every attachment list, every File form and every Attach field, and it is the WRITE that is the lock.
	"File": {
		1: {
			"System Manager": (1, 1),
			"All": (1, 0),
		}
	},
}

# Upstream fields reclassified to permlevel 1 via Property Setter (the non-fork way to change another app's field), paired with the FIELD_LEVELS grant above.
_PERMLEVEL_1_FIELDS = {
	"LMS Test Case": ("input", "expected_output"),
	"LMS Program Member": ("full_name", "progress"),
	"Insights Data Source v3": (
		"connection_string",
		"bigquery_service_account_key",
		"http_headers",
		"api_custom_headers",
	),
	# Both are written into every wiki page unescaped; `javascript` is the same injection as `head_html`.
	"Wiki Settings": ("head_html", "javascript"),
	"LMS Batch": ("custom_script", "custom_component"),
	# Computed from the bytes and marked read_only, which frappe does NOT enforce server-side: a save rewrote all three (a forged hash, a forged size, and a file_url pointing anywhere).
	"File": ("content_hash", "file_size", "file_url"),
}

# BASELINE floor trims: a stray grant on the auto-inherited `Desk User` role that lets any System User
# enumerate a sensitive core doctype via get_list. `select` alone lists rows (and User is a CORE_DOCTYPE, so
# permlevel cannot hide its columns), so a rep pulls the whole staff directory. Stripped surgically — only
# this flag on this role — leaving every other row (LMS staff, System Manager) exactly as frappe/lms set it.
BASELINE_ROLE_TRIMS = {
	"User": {"Desk User": {"select": 0}},
}


def apply_baseline_role_trims():
	"""Strip the BASELINE_ROLE_TRIMS grants. Surgical `update_permission_property`, never a matrix rebuild —
	we do not own User's matrix, we only correct one inherited flag and let frappe's engine enforce the rest."""
	for doctype, roles in BASELINE_ROLE_TRIMS.items():
		if not frappe.db.exists("DocType", doctype):
			continue
		for role, flags in roles.items():
			if not frappe.db.exists("Custom DocPerm", {"parent": doctype, "role": role, "permlevel": 0}):
				continue
			for ptype, value in flags.items():
				update_permission_property(doctype, role, 0, ptype, value, validate=False)
	frappe.clear_cache()


def apply_field_levels():
	"""Grant the permlevel rows FIELD_LEVELS declares. `add_permission` copies the doctype's stock matrix
	into Custom DocPerm first (frappe.permissions.copy_perms, every flag), so adding a level never costs a
	role the access crm granted it — the trap that makes a hand-rolled Custom DocPerm row catastrophic."""
	for doctype, levels in FIELD_LEVELS.items():
		if not frappe.db.exists("DocType", doctype):
			continue
		for permlevel, roles in levels.items():
			for role, (read, write) in roles.items():
				if not frappe.db.exists(
					"Custom DocPerm", {"parent": doctype, "role": role, "permlevel": permlevel}
				):
					add_permission(doctype, role, permlevel)
				for ptype, value in (("read", read), ("write", write)):
					update_permission_property(doctype, role, permlevel, ptype, value, validate=False)
	frappe.clear_cache()


def apply_field_permlevels():
	"""Bump _PERMLEVEL_1_FIELDS to permlevel 1 via Property Setter (idempotent upsert) — the non-fork way
	to reclassify an upstream field so the FIELD_LEVELS grant can hide it."""
	for doctype, fields in _PERMLEVEL_1_FIELDS.items():
		if not frappe.db.exists("DocType", doctype):
			continue
		for fieldname in fields:
			frappe.make_property_setter(
				{
					"doctype": doctype,
					"fieldname": fieldname,
					"property": "permlevel",
					"value": 1,
					"property_type": "Int",
				},
				is_system_generated=True,
			)
	frappe.clear_cache()


def _app_parent_doctypes(app_name):
	"""Parent doctypes belonging to `app_name`. Children inherit their parent and singles carry no matrix."""
	modules = frappe.get_all("Module Def", filters={"app_name": app_name}, pluck="name")
	if not modules:
		return []
	return frappe.get_all(
		"DocType", filters={"module": ["in", modules], "istable": 0, "issingle": 0}, pluck="name"
	)


def rebuild_targets():
	"""doctype -> the rows it must be rebuilt to. THE one reader of the ledger on the write path.

	Two sources, and the second is the inversion. Always: every doctype the ledger OPENs, which is what
	LOCKED_MATRIX did. Additionally: every parent doctype of an app in ENFORCED_APPS, which resolves to
	DENIED unless OPEN names it — so a doctype nobody classified is closed rather than left at whatever
	its app shipped. tatva_connect is skipped throughout: our own doctypes declare their permissions in
	their own JSON (POLICY §6), and rebuilding them here would silently overwrite that declaration."""
	targets = {dt: ledger.rows_for(dt) for dt in ledger.OPEN}
	for app_name in ENFORCED_APPS:
		if app_name == "tatva_connect":
			continue
		for doctype in _app_parent_doctypes(app_name):
			targets.setdefault(doctype, ledger.rows_for(doctype))
	return {dt: rows for dt, rows in targets.items() if frappe.db.exists("DocType", dt)}


def apply(*_args, **_kwargs):
	"""Rebuild each governed doctype's permission matrix to exactly what the ledger declares (after_migrate)."""
	for doctype, roles in rebuild_targets().items():
		if not frappe.db.exists("DocType", doctype):
			continue
		reset_perms(doctype)  # drop any prior custom perms -> deterministic rebuild
		for role, perms in roles.items():
			r, w, c, d = perms[:4]
			if_owner = perms[4] if len(perms) > 4 else 0  # optional 5th element (own-records-only)
			submittable = perms[5] if len(perms) > 5 else 0  # optional 6th: submit/cancel/amend, together
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
					"submit": submittable,
					"cancel": submittable,
					"amend": submittable,
				}
			).insert(
				ignore_permissions=True
			)  # authz-ok: tier-a — permission scaffolding, runs in schema setup
	apply_field_levels()
	apply_field_permlevels()
	apply_app_security_settings()
	apply_unpublished_web_forms()
	apply_baseline_role_trims()
	frappe.clear_cache()


def apply_app_security_settings():
	"""Pin APP_SECURITY_SETTINGS. Idempotent, and a no-op on a site where the app is not installed."""
	for doctype, values in APP_SECURITY_SETTINGS.items():
		if not frappe.db.exists("DocType", doctype):
			continue
		for field, value in values.items():
			if frappe.db.get_single_value(doctype, field) != value:
				frappe.db.set_single_value(doctype, field, value)


def apply_unpublished_web_forms():
	"""Unpublish UNPUBLISHED_WEB_FORMS. Idempotent; skips a form this site does not have."""
	for name in UNPUBLISHED_WEB_FORMS:
		if frappe.db.exists("Web Form", name) and frappe.db.get_value("Web Form", name, "published"):
			frappe.db.set_value("Web Form", name, "published", 0, update_modified=False)


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
	"""Fail the migrate if a governed doctype is EFFECTIVELY open to All/Guest write/create/delete
	— the Layer-4 drift guard, same idiom as automation.drift / notifications.drift.

	Reads the same rebuild_targets() the write path does, so arming an app in ENFORCED_APPS extends the
	guard with it and the two can never describe different sets of doctypes."""
	bad = [grant for doctype in rebuild_targets() for grant in effective_all_guest_grants(doctype)]
	if bad:
		frappe.throw(_("Permission lockdown drift — locked doctypes open to All/Guest: {0}").format(bad))


def assert_ledger_parity(*_args, **_kwargs):
	"""Fail the migrate if the ledger and the frozen LOCKED_MATRIX have diverged.

	LOCKED_MATRIX is no longer read by apply(); it is kept as the reviewed reference the ledger was
	seeded from, and this asserts the swap stayed a no-op. It retires the day the first app is armed in
	ENFORCED_APPS and the ledger legitimately says more than the matrix ever did."""

	def pad(perms):
		return tuple(perms[:5]) + (0,) * (5 - len(perms[:5]))

	drift = []
	for doctype in set(LOCKED_MATRIX) | set(ledger.OPEN):
		want = {r: pad(p) for r, p in LOCKED_MATRIX.get(doctype, {}).items()}
		have = {r: pad(p) for r, p in ledger.rows_for(doctype).items()} if doctype in ledger.OPEN else {}
		if want != have:
			drift.append(doctype)
	if drift:
		frappe.throw(_("Ledger drifted from LOCKED_MATRIX: {0}").format(sorted(drift)))
