# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The RESTRICTION LEDGER — the ONE declaration of what a doctype is allowed to be.

THE MODEL. Every parent doctype resolves to exactly one BUCKET, and the bucket fixes its
`(role, read, write, create, delete[, if_owner])` rows. There are six buckets and nothing invents a
seventh. A doctype absent from `OPEN` is **DENIED**.

THAT ABSENCE IS THE WHOLE POINT. `LOCKED_MATRIX` listed what we had locked, so a doctype nobody had
looked at stayed open at whatever posture its app shipped — and an audit walks exactly that list.
This file lists what we have OPENED, so a doctype nobody has looked at is closed, and an app upgrade
lands closed instead of open. Same engine, same rebuild; the default is inverted.

WHY THIS FILE IMPORTS NOTHING FROM FRAPPE. `hooks.py` is read while the framework is still booting,
so a `frappe` import at module scope here would break the site. Everything that needs frappe — the
live schema, the current matrix, the diff — lives in `ledger_audit.py` and is called, never imported
at module scope. Keep this file pure data and pure functions.

WHAT THIS FILE IS NOT. It does not decide which ROWS of a doctype a role may see (that is
`visibility.py` + `permission_query_conditions`), nor which FIELDS (that is `entitlement.py` and
`lockdown.FIELD_LEVELS`). This is the doctype-level gate only, exactly as `lockdown.py` always was.

STATUS — PHASE 1. Nothing here is applied. `lockdown.apply()` still reads `LOCKED_MATRIX`; the two
are asserted identical by `tests/access/test_ledger.py` so that Phase 2 is provably a no-op before it
is ever a change. `TIER0` and `EXECUTABLE_LAW` are declarations under review, not yet enforced.
"""

from tatva_connect.whatsapp.roles import WHATSAPP_ADMIN, WHATSAPP_USER

SYSTEM_MANAGER = "System Manager"
SALES_MANAGER = "Sales Manager"
SALES_USER = "Sales User"
ALL = "All"  # every authenticated login silently holds this — see POLICY §5

# bucket -> {role: (r, w, c, d[, if_owner[, submit]])} — POLICY §4.
BUCKETS = {
	# 1 · records a rep works on daily; reps never delete.
	"OPERATIONAL": {
		SYSTEM_MANAGER: (1, 1, 1, 1),
		SALES_MANAGER: (1, 1, 1, 1),
		SALES_USER: (1, 1, 1, 0),
	},
	# 2 · masters/layouts a rep reads and a manager curates.
	"CONFIG": {
		SYSTEM_MANAGER: (1, 1, 1, 1),
		SALES_MANAGER: (1, 1, 1, 1),
		SALES_USER: (1, 0, 0, 0),
	},
	# 3 · integrations / automation / settings; no rep row at all.
	"ADMIN": {
		SYSTEM_MANAGER: (1, 1, 1, 1),
		SALES_MANAGER: (1, 1, 1, 1),
	},
	# Read-only reference data everyone needs — the ONLY sanctioned non-if_owner `All` grant (POLICY §5 rule 1).
	"REFERENCE": {
		SYSTEM_MANAGER: (1, 1, 1, 1),
		ALL: (1, 0, 0, 0),
	},
	# A user's own records, and only those — the second sanctioned `All` shape (POLICY §5 rule 2).
	"OWN": {
		SYSTEM_MANAGER: (1, 1, 1, 1),
		ALL: (1, 1, 1, 0, 1),
	},
	# The resting state. Read-only for an admin so an existing embed still renders while it is migrated off.
	"DENIED": {
		SYSTEM_MANAGER: (1, 0, 0, 0),
	},
}

DEFAULT_BUCKET = "DENIED"

# --- Tier 0 · the escalation set — reported, never opened without review ------------------------
TIER0 = (
	"User",
	"Has Role",
	"Role",
	"Role Profile",
	"User Permission",
	"DocShare",
	"Custom DocPerm",
	"DocType",
	"Custom Field",
	"Property Setter",
	"Client Script",
)

# --- R2 · executable by contract, so authorship is the only control ------------------------------
EXECUTABLE_FIELDTYPES = ("Code",)

# Markup reaching a browser — sanitised by fieldtype, never by name.
MARKUP_FIELDTYPES = ("HTML", "HTML Editor", "Text Editor", "Markdown Editor")

# --- OPEN · seeded verbatim from LOCKED_MATRIX. Value = bucket name, or explicit {role: tuple}. ---

_CRM_CORE = {
	"Contact": {
		SYSTEM_MANAGER: (1, 1, 1, 1),
		SALES_MANAGER: (1, 1, 1, 1),  # managers may delete (clean up duplicates/junk)
		SALES_USER: (1, 1, 1, 0),
	},
	# VAPT P1 IDOR — edit/delete only your OWN comment; create/read run elsewhere.
	"Comment": {
		SYSTEM_MANAGER: (1, 1, 1, 1),
		"Website Manager": (1, 1, 1, 1),  # preserve the stock grant (Custom DocPerm overrides stock)
		ALL: (0, 1, 0, 1, 1),
	},
	# media_for gates on the CALL LOG, so the modal needs no role grant here.
	"CRM Call Media": {SYSTEM_MANAGER: (1, 1, 1, 1)},
	# Code execution in the viewer's session — killed.
	"Custom HTML Block": "DENIED",
}

# Agent-only internal; no customer portal (decision 2026-07-09).
_HELPDESK = {
	"HD Ticket": {SYSTEM_MANAGER: (1, 1, 1, 1), "Agent": (1, 1, 1, 1), "Agent Manager": (1, 1, 1, 1)},
	"HD Article": {SYSTEM_MANAGER: (1, 1, 1, 1), "Agent": (1, 1, 1, 1), "Agent Manager": (1, 1, 1, 1)},
	"HD Article Category": {
		SYSTEM_MANAGER: (1, 1, 1, 1),
		"Agent": (1, 1, 1, 1),
		"Agent Manager": (1, 1, 1, 1),
	},
	"HD Article Feedback": {
		SYSTEM_MANAGER: (1, 1, 1, 1),
		"Agent": (1, 1, 1, 1),
		"Agent Manager": (1, 1, 1, 1),
	},
	"HD View": {SYSTEM_MANAGER: (1, 1, 1, 1), "Agent": (1, 1, 1, 1), "Agent Manager": (1, 1, 1, 1)},
	# `about` is HTML authored for every colleague — Agent keeps read only.
	"HD Ticket Template": {
		SYSTEM_MANAGER: (1, 1, 1, 1),
		"Agent Manager": (1, 1, 1, 1),
		"Agent": (1, 0, 0, 0),
	},
}

# Capability doctypes, read-only — a `create` grant would bypass the send gate.
_WHATSAPP = {
	"WhatsApp Message": {
		SYSTEM_MANAGER: (1, 1, 1, 1),
		WHATSAPP_USER: (1, 0, 0, 0),
		WHATSAPP_ADMIN: (1, 0, 0, 0),
	},
	"WhatsApp Templates": {
		SYSTEM_MANAGER: (1, 1, 1, 1),
		WHATSAPP_USER: (1, 0, 0, 0),
		WHATSAPP_ADMIN: (1, 0, 0, 0),
	},
	"WhatsApp Account": {SYSTEM_MANAGER: (1, 1, 1, 1), WHATSAPP_ADMIN: (1, 1, 1, 1)},
	"WhatsApp Settings": {SYSTEM_MANAGER: (1, 1, 1, 1), WHATSAPP_ADMIN: (1, 1, 1, 1)},
}

# Internal handbook, login-only; stock ships the public docs-site surface we do not have.
_WIKI = {
	# Drops the Guest row: an anonymous caller could create AND edit ratings.
	"Wiki Feedback": {SYSTEM_MANAGER: (1, 1, 1, 1), "Wiki Approver": (1, 1, 1, 1), "Wiki User": (0, 0, 1, 0)},
	# Legacy contribution flow, superseded by Wiki Change Request; the 6th element carries submit/cancel/amend.
	"Wiki Page Patch": {
		SYSTEM_MANAGER: (1, 1, 1, 1, 0, 1),
		"Wiki Approver": (1, 1, 1, 1, 0, 1),
		ALL: (1, 1, 1, 1, 1),
	},
}

OPEN = {**_CRM_CORE, **_HELPDESK, **_WHATSAPP, **_WIKI}


def bucket_for(doctype):
	"""The bucket name this doctype resolves to, or None when OPEN gives it explicit rows."""
	entry = OPEN.get(doctype, DEFAULT_BUCKET)
	return entry if isinstance(entry, str) else None


def rows_for(doctype):
	"""The `{role: tuple}` this doctype must be rebuilt to — THE one reader of OPEN.

	A doctype absent from OPEN resolves to DEFAULT_BUCKET, which is how the inversion is expressed:
	the caller asks about any doctype and always gets an answer, and that answer is closed."""
	entry = OPEN.get(doctype, DEFAULT_BUCKET)
	return dict(BUCKETS[entry]) if isinstance(entry, str) else dict(entry)


def is_declared(doctype):
	"""Has a human deliberately opened this doctype? False means it resolves to DENIED by default."""
	return doctype in OPEN


def grants_all_write(rows):
	"""Does this row set hand `All`/`Guest` a non-if_owner write? That is the breach shape (POLICY §5).

	An if_owner grant is own-records-only and sanctioned; only an unscoped one opens every row."""
	for role, perms in rows.items():
		if role not in (ALL, "Guest"):
			continue
		write, create, delete = perms[1], perms[2], perms[3]
		if_owner = perms[4] if len(perms) > 4 else 0
		if (write or create or delete) and not if_owner:
			return True
	return False
