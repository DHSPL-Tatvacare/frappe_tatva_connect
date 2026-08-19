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
ledger names it.

Runs on after_migrate rather than as a patch, for the SAME reason as schema_setup: install-app baselines
patches.txt WITHOUT running it, so a fresh DB must get the lock here. It rebuilds a doctype only when
`declaration_hash` moves, so a deploy that changes no declaration writes no permission row and an
operator's Desk edit stands — the contract frappe's own `migration_hash` gives a DocType JSON.

Row-scope (which records within a doctype a role may see) is a SEPARATE layer — User Permission +
the visibility brain — not this module. This module is purely the doctype-level gate.
"""

import hashlib
import json

import frappe
from frappe import _
from frappe.permissions import add_permission, reset_perms, update_permission_property

from tatva_connect.access import ledger

# Apps fully inverted — every parent doctype DENIED unless the ledger opens it; EMPTY means no inversion runs and an unnamed doctype keeps its own app's posture.
ENFORCED_APPS = ()

# Security switches another app owns and ships permissive. Pinned on after_migrate, same reason as the ledger.
APP_SECURITY_SETTINGS = {
	# Insights queries the SITE DB directly, so with this OFF every Insights user reads every `tab*`, CRM Lead included.
	"Insights Settings": {"enable_permissions": 1, "apply_user_permissions": 1},
}

# Doctypes that must never load from a spreadsheet. `DataImport.validate_doctype` refuses on a falsy `allow_import` BEFORE any permission check and a System Manager does NOT bypass that line, so this one flag closes the Desk importer and the SPA's menu together; it is set through a Property Setter because these doctypes belong to the crm app, which is the override frappe reads itself (`utils/user.py` builds `can_import` from DocType rows AND Property Setter rows). CRM LEAD IS DELIBERATELY ABSENT - the flag is doctype-wide and the bulk lead load is the one import the business runs, so naming it here would kill that load at Desk too.
IMPORT_OFF = (
	"CRM Deal",
	"CRM Task",
	"CRM Call Log",
	"CRM Organization",
	"FCRM Note",
	"CRM Product",
	"CRM Territory",
	"CRM Industry",
	"CRM Lead Source",
	"CRM Sales Hierarchy",
)

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
	# Copied verbatim into the Web Form the public fills in, so this script runs in an anonymous visitor's browser.
	"CRM Intake Form": {
		1: {
			"System Manager": (1, 1),
		}
	},
	"CRM Lead": {
		1: {
			"System Manager": (1, 1),
			"Sales Manager": (1, 1),
			"Sales User": (1, 0),
		}
	},
	# The deal's grain is stamped from its lead and carries the same permlevel, so it needs the same grant or a rep sees no product line on the deal at all.
	"CRM Deal": {
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
			"Course Creator": (1, 1),
		}
	},
	# A program's member table names every colleague and their progress; the parent's grant governs it.
	"LMS Program": {
		1: {
			"System Manager": (1, 1),
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
	# The switches the whole team layer rests on, and the origin allowlist that lets an outside site embed.
	"Insights Settings": {
		1: {
			"System Manager": (1, 1),
		}
	},
	# Head HTML is written into every wiki page unescaped, and the GitHub App credentials are plaintext beside it.
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
	# `User` at permlevel 0 is NOT a directory. Frappe puts the credentials there (api_key, api_secret,
	# roles, restrict_ip) but leaves a colleague's phone, date of birth, last IP, last login and live
	# sessions readable by anyone who may read the row at all. A picker needs a name, an email and an
	# avatar; it does not need a rep's mobile number. These move up so the grant below can be a directory
	# grant and nothing more — and they become admin-only for every role that reads User today, which is a
	# reduction in exposure, not an addition.
	"User": (
		"mobile_no", "phone", "birth_date",
		"last_ip", "last_login", "last_active", "last_password_reset_date",
		"active_sessions", "simultaneous_sessions", "logout_all_sessions",
		"bypass_restrict_ip_check_if_2fa_enabled", "new_password",
	),
	"LMS Test Case": ("input", "expected_output"),
	"LMS Program Member": ("full_name", "progress"),
	"Insights Data Source v3": (
		"connection_string",
		"bigquery_service_account_key",
		"http_headers",
		"api_custom_headers",
	),
	# Two switches unbind Insights from this ledger, one authorises an outside embed, one copies tables onto the VM disk.
	"Insights Settings": (
		"enable_permissions",
		"apply_user_permissions",
		"allowed_origins",
		"enable_data_store",
	),
	# The first two are written into every wiki page unescaped; the rest are the GitHub App's credentials, in clear.
	"Wiki Settings": (
		"head_html",
		"javascript",
		"github_app_client_secret",
		"github_app_private_key",
		"github_webhook_secret",
	),
	"LMS Batch": ("custom_script", "custom_component"),
	# Computed from the bytes and marked read_only, which frappe does NOT enforce server-side: a save rewrote all three (a forged hash, a forged size, and a file_url pointing anywhere).
	"File": ("content_hash", "file_size", "file_url"),
}

# BASELINE floor trims: a stray grant on the auto-inherited `Desk User` role that lets any System User
# enumerate a sensitive core doctype via get_list. `select` alone lists rows (and User is a CORE_DOCTYPE, so
# permlevel cannot hide its columns), so a rep pulls the whole staff directory. Stripped surgically — only
# this flag on this role — leaving every other row (LMS staff, System Manager) exactly as frappe/lms set it.
BASELINE_ROLE_TRIMS = {
	# lms's ONLY test for "is this person staff" is the Moderator role, so it stays; its write+create on User is a route to System Manager, so that goes.
	"User": {"Desk User": {"select": 0}, "Moderator": {"write": 0, "create": 0}},
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


def apply_import_lock():
	"""Turn `allow_import` off for IMPORT_OFF via Property Setter (idempotent upsert) - the non-fork way to close another app's importer, and the same mechanism `apply_field_permlevels` uses above."""
	for doctype in IMPORT_OFF:
		if not frappe.db.exists("DocType", doctype):
			continue
		frappe.make_property_setter(
			{
				"doctype": doctype,
				"doctype_or_field": "DocType",
				"property": "allow_import",
				"value": 0,
				"property_type": "Check",
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

	Two sources, and the second is the inversion. Always: every doctype the ledger OPENs, ours included.
	Additionally: every parent doctype of an app in ENFORCED_APPS, which resolves to DENIED unless OPEN
	names it — so a doctype nobody classified is closed rather than left at whatever its app shipped.
	tatva_connect is skipped only in that SWEEP: naming our doctype in OPEN governs it, but arming our own
	app would close every internal doctype nobody has classified yet."""
	targets = {dt: ledger.rows_for(dt) for dt in ledger.OPEN}
	for app_name in ENFORCED_APPS:
		if app_name == "tatva_connect":
			continue
		for doctype in _app_parent_doctypes(app_name):
			targets.setdefault(doctype, ledger.rows_for(doctype))
	return {dt: rows for dt, rows in targets.items() if frappe.db.exists("DocType", dt)}


def declaration_hash(doctype):
	"""A doctype's declared rows as one stable string — the key `apply()` decides on."""
	declared = {"rows": ledger.rows_for(doctype), "extras": ledger.extra_ptypes_for(doctype)}
	return hashlib.sha256(json.dumps(declared, sort_keys=True, default=list).encode()).hexdigest()


def apply(*_args, **_kwargs):
	"""Rebuild a governed doctype's matrix to what the ledger declares, when that declaration changes.

	Gated on `declaration_hash` and not run every migrate, for the reason frappe gates a DocType JSON on
	`migration_hash`: rebuilding unconditionally would delete an operator's Desk edit on every deploy."""
	for doctype, roles in rebuild_targets().items():
		if not frappe.db.exists("DocType", doctype):
			continue
		key = f"ledger_hash:{doctype}"
		digest = declaration_hash(doctype)
		if frappe.db.get_default(key) == digest:
			continue
		extras = ledger.extra_ptypes_for(doctype)
		reset_perms(doctype)  # drop any prior custom perms -> deterministic rebuild
		for role, perms in roles.items():
			r, w, c, d = perms[:4]
			if_owner = perms[4] if len(perms) > 4 else 0  # optional 5th element (own-records-only)
			submittable = perms[5] if len(perms) > 5 else 0  # optional 6th: submit/cancel/amend, together
			shareable = perms[6] if len(perms) > 6 else 0  # optional 7th: `share`, which EXTRA_PTYPES cannot scope
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
					"share": shareable,
					# tail rights ride the role that already reads; everything unnamed stays 0
					**{ptype: (1 if r else 0) for ptype in extras},
				}
			).insert(
				# authz-ok: tier-a — permission scaffolding, runs in schema setup
				ignore_permissions=True
			)
		frappe.db.set_default(key, digest)
	apply_field_levels()
	apply_field_permlevels()
	apply_import_lock()
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
