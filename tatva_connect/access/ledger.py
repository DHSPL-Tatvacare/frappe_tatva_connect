# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The RESTRICTION LEDGER — the ONE declaration of what a doctype is allowed to be.

THE MODEL. Every parent doctype resolves to exactly one BUCKET, and the bucket fixes its
`(role, read, write, create, delete[, if_owner])` rows. A doctype absent from `OPEN` is **DENIED**, and a
doctype that needs a shape no bucket has spells its rows out rather than growing the list of buckets.

THAT ABSENCE IS THE WHOLE POINT — WHERE IT IS ARMED. A matrix that lists what we LOCKED leaves a doctype
nobody looked at open at whatever posture its app shipped, and an audit walks exactly that list. This file
lists what we have OPENED instead, and `rows_for` answers DENIED for everything else.

But an answer is not a row. `lockdown.apply()` WRITES only what `rebuild_targets()` names — this file's
doctypes, plus every doctype of an app armed in `lockdown.ENFORCED_APPS`. So absence closes a doctype only
for an armed app; elsewhere the doctype keeps the posture its app shipped and this file's answer for it is
advisory. Read `ENFORCED_APPS` before relying on the inversion, and see LOCKDOWN.md §1 for what carries the
surfaces it does not reach.

THE ONE LANE. A permission is a DocPerm row, written either from a DocType JSON or as Custom DocPerm. The
JSON is only ours to edit when the app is ours, and most of this site is frappe's, helpdesk's, wiki's and
LMS's. So this file declares every doctype we govern, OURS INCLUDED, `lockdown.apply()` writes it as Custom
DocPerm, and nothing else in this app declares a doctype-level permission.

APPLIED WHEN IT CHANGES, NOT EVERY MIGRATE. `apply()` hashes each doctype's rows and rewrites only when
that hash moves, the same contract frappe's `migration_hash` gives a DocType JSON — so an operator's Desk
edit survives every migrate until this file says otherwise.

WHY THIS FILE IMPORTS NOTHING FROM FRAPPE. `hooks.py` is read while the framework is still booting,
so a `frappe` import at module scope here would break the site. Everything that needs frappe — the
live schema, the current matrix, the diff — lives in `ledger_audit.py` and is called, never imported
at module scope. Keep this file pure data and pure functions.

WHAT THIS FILE IS NOT. It does not decide which ROWS of a doctype a role may see (that is
`visibility.py` + `permission_query_conditions`), nor which FIELDS (that is `entitlement.py` and
`lockdown.FIELD_LEVELS`). This is the doctype-level gate only, exactly as `lockdown.py` always was.

WHAT A ROW GRANTS. read/write/create/delete (+ if_owner, + submit, + share), so print, select and import
are 0 everywhere. `report` is not among them: it returns the SAME rows a read returns, grouped, through the
same query conditions and permlevels, so `lockdown` gives it to every reader and no doctype declares it.
The rights that do differ per doctype — the ones that send or remove data — live in `EXTRA_PTYPES`.
`TIER0` is a watchlist, not a grant.
"""

from tatva_connect.whatsapp.roles import WHATSAPP_ADMIN, WHATSAPP_USER

SYSTEM_MANAGER = "System Manager"
SALES_MANAGER = "Sales Manager"
SALES_USER = "Sales User"
AUTOMATION_MANAGER = "Automation Manager"  # ships in fixtures/role.json, assigned to nobody
AGENT = "Agent"                            # helpdesk
LMS_STUDENT = "LMS Student"                # lms; Moderator holds no doctype grant here, only lms's staff test
COURSE_CREATOR = "Course Creator"
BATCH_EVALUATOR = "Batch Evaluator"
AGENT_MANAGER = "Agent Manager"
MODERATOR = "Moderator"                    # lms; holds exactly ONE grant — the directory read below
WIKI_MANAGER = "Wiki Manager"              # wiki; Wiki Approver is deliberately absent — there is no review tier
WIKI_USER = "Wiki User"                    # wiki latches this onto every new User (wiki/hooks.py after_insert)
INSIGHTS_ADMIN = "Insights Admin"          # insights; authors dashboards, and `is_admin` already reads System Manager as one
INSIGHTS_USER = "Insights User"            # insights; the app's front door — every endpoint defaults to this role
ALL = "All"  # every authenticated login silently holds this — see POLICY §5
DESK_USER = "Desk User"  # automatic (permissions.py:40) — ungrantable, but a DocPerm row naming it means "any staff login"

# bucket -> {role: (r, w, c, d[, if_owner[, submit[, share]]])} — POLICY §4.
BUCKETS = {
	# 1 · records a rep works on daily; reps never delete. The 7th flag is `share`, which crm ships on all three roles: frappe's assign_to falls back to a DocShare whenever the assignee cannot already see the row, and that fallback runs as the ASSIGNER, so dropping it breaks assignment outright.
	"OPERATIONAL": {
		SYSTEM_MANAGER: (1, 1, 1, 1, 0, 0, 1),
		SALES_MANAGER: (1, 1, 1, 1, 0, 0, 1),
		SALES_USER: (1, 1, 1, 0, 0, 0, 1),
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
	# 4 · masters a manager curates; only a System Manager deletes — a dead Link target orphans history.
	"MASTER": {
		SYSTEM_MANAGER: (1, 1, 1, 1),
		SALES_MANAGER: (1, 1, 1, 0),
		SALES_USER: (1, 0, 0, 0),
		AUTOMATION_MANAGER: (1, 0, 0, 0),
	},
	# 5 · the platform's shape. Every role that authors a grain-scoped record reads it, or its picker is empty.
	"PLATFORM_READ": {
		SYSTEM_MANAGER: (1, 1, 1, 1),
		SALES_MANAGER: (1, 0, 0, 0),
		SALES_USER: (1, 0, 0, 0),
		AUTOMATION_MANAGER: (1, 0, 0, 0),
		WHATSAPP_ADMIN: (1, 0, 0, 0),
		AGENT_MANAGER: (1, 0, 0, 0),
	},
	# 6 · credentials and site wiring; every runtime reader goes through db.get_value, which asks nothing.
	"PLATFORM": {
		SYSTEM_MANAGER: (1, 1, 1, 1),
	},
	# 7 · what an automation author configures. Not a Sales grant: a manager who needs it is given the role.
	"AUTOMATION": {
		SYSTEM_MANAGER: (1, 1, 1, 1),
		AUTOMATION_MANAGER: (1, 1, 1, 1),
	},
	# 7b · helpdesk work; delete_contact/customer/ticket/category are all agent-callable, so agents delete.
	"HD_WORK": {
		SYSTEM_MANAGER: (1, 1, 1, 1),
		AGENT_MANAGER: (1, 1, 1, 1),
		AGENT: (1, 1, 1, 1),
	},
	# 7c · helpdesk config an agent reads and a manager curates; nobody but an admin deletes.
	"HD_CONFIG": {
		SYSTEM_MANAGER: (1, 1, 1, 1),
		AGENT_MANAGER: (1, 1, 1, 0),
		AGENT: (1, 0, 0, 0),
	},
	# 7d · the wiki's authoring machinery; a Change Request is how a page is SAVED here, not a review step.
	"WIKI": {
		SYSTEM_MANAGER: (1, 1, 1, 1),
		WIKI_MANAGER: (1, 1, 1, 1),
	},
	# 7e · what a reader touches. Everyone reads the handbook; only the wiki's own manager writes it.
	"WIKI_READ": {
		SYSTEM_MANAGER: (1, 1, 1, 1),
		WIKI_MANAGER: (1, 1, 1, 1),
		WIKI_USER: (1, 0, 0, 0),
	},
	# 7f · what an Insights author builds. Which dashboards a consumer then SEES is the team layer, not this.
	"INSIGHTS": {
		SYSTEM_MANAGER: (1, 1, 1, 1),
		INSIGHTS_ADMIN: (1, 1, 1, 1),
	},
	# 7g · what a consumer opens. `execute` and `track_view` run through check_permission("read"), so read is enough.
	"INSIGHTS_READ": {
		SYSTEM_MANAGER: (1, 1, 1, 1),
		INSIGHTS_ADMIN: (1, 1, 1, 1),
		INSIGHTS_USER: (1, 0, 0, 0),
	},
	# 8 · what an automation author READS — append-only trails and the logs their workspaces render.
	"AUTOMATION_LOG": {
		SYSTEM_MANAGER: (1, 1, 1, 1),
		AUTOMATION_MANAGER: (1, 0, 0, 0),
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

# The Tier 0 entries reviewed and deliberately opened — "without review" above is what this records.
TIER0_REVIEWED = {
	"User": "directory READ; credentials and PII sit at permlevel 1 (lockdown._PERMLEVEL_1_FIELDS)",
	"User Permission": (
		"grain scoping, which a manager sets on every person they bring in — the axes are "
		"entitlement._UP_AXES and nothing else is granted this way. Opened to Sales Manager because it "
		"is an ordinary act of staffing a team, not an administrative one. It is Tier 0 for a real "
		"reason: a row DELETED is a widening, so a manager can lift their own scope. That is accepted — "
		"a manager already reads their whole line — and it is why the grant stops at Sales Manager."
	),
}

# --- R2 · executable by contract, so authorship is the only control ------------------------------
EXECUTABLE_FIELDTYPES = ("Code",)

# Markup reaching a browser — sanitised by fieldtype, never by name.
MARKUP_FIELDTYPES = ("HTML", "HTML Editor", "Text Editor", "Markdown Editor")

# --- OPEN · value = a bucket name, or explicit {role: tuple} when no bucket has the shape. ---

# The platform's own user row. TIER0 still — nobody creates or edits a User but an administrator — but a
# READ is not an edit, and two apps read this doctype DIRECTLY rather than through a gated method: helpdesk
# resolves agents and LMS resolves discussion authors. CRM does not appear here because it never needed to:
# `crm.api.session.get_users` is a whitelisted method with its own role gate, which is why the SPA's assignee
# pickers work with no DocPerm at all — the pattern to copy when a third app wants a directory.
#
# `Desk User` is deliberately ABSENT. Frappe itself ships that row at 0 (core/doctype/user/user.json), so
# granting it would widen past the platform's own default for every staff login, to serve two apps.
#
# Safe because the row a reader sees is a DIRECTORY: the credentials sit at permlevel 1 where frappe put
# them, and `lockdown._PERMLEVEL_1_FIELDS["User"]` moves the PII and session columns up to join them.
# The readers below are NOT CRM grants: rebuilding this matrix deleted frappe's own `Desk User: select` row,
# which is what resolved a colleague in every Link picker, so anyone who assigns or names a person had no way
# to pick one — the list rendered empty for exactly the people who were given the form. This replaces that
# floor for the roles that actually hold such a field and for no one else, which `test_ledger_reachability`
# is the standing check on: a rep names an owner, an assignee, a caller and a contact's user (CRM Lead,
# CRM Task, CRM Call Log, CRM Deal, Contact, CRM Smart View, CRM Telephony Agent, CRM View Settings), an
# agent names one on five helpdesk forms, and Automation Manager names a partner user on a mapping.
_PLATFORM_USER = {
	"User": {
		SYSTEM_MANAGER: (1, 1, 1, 1),
		SALES_MANAGER: (1, 0, 0, 0),
		SALES_USER: (1, 0, 0, 0),
		AUTOMATION_MANAGER: (1, 0, 0, 0),
		AGENT_MANAGER: (1, 0, 0, 0),
		AGENT: (1, 0, 0, 0),
		MODERATOR: (1, 0, 0, 0),
	},
}

_CRM_CORE = {
	# Records a rep works on all day. Reps never delete; a manager clears duplicates and junk.
	# Support reaches these through the roster's `Lead Viewer` add-on (Sales Manager), not through a helpdesk row: crm scopes Lead/Deal by SALES ownership, so an Agent grant here reads zero rows and would be a permission that grants nothing.
	"CRM Lead": "OPERATIONAL",
	"CRM Deal": "OPERATIONAL",
	"CRM Task": "OPERATIONAL",
	"CRM Call Log": "OPERATIONAL",
	"FCRM Note": "OPERATIONAL",
	"CRM Organization": "OPERATIONAL",
	# One person, two desks — a CRM contact becomes the customer helpdesk serves, so both roles hold it.
	"Contact": {
		SYSTEM_MANAGER: (1, 1, 1, 1, 0, 0, 1),
		SALES_MANAGER: (1, 1, 1, 1, 0, 0, 1),
		SALES_USER: (1, 1, 1, 0, 0, 0, 1),
		AGENT_MANAGER: (1, 1, 1, 1),
		AGENT: (1, 1, 1, 1),
	},
	# A rep saves and deletes their OWN list views — per-user state, not shared configuration.
	"CRM View Settings": {
		SYSTEM_MANAGER: (1, 1, 1, 1),
		SALES_MANAGER: (1, 1, 1, 1),
		SALES_USER: (1, 1, 1, 1),
	},
	# Master data and pipeline configuration a manager runs the business on.
	"CRM Picklist Value": "MASTER",
	"CRM Task Option": "MASTER",
	"CRM Task Type": "MASTER",
	"CRM Task Section": "MASTER",
	"CRM Task Field": "MASTER",
	"CRM Lead Stage": "MASTER",
	"CRM Side Effect Option": "MASTER",
	"CRM Lead Status": "MASTER",
	"CRM Deal Status": "MASTER",
	"CRM Lead Source": "MASTER",
	"CRM Lost Reason": "MASTER",
	"CRM Industry": "MASTER",
	"CRM Territory": "MASTER",
	"CRM Product": "MASTER",
	"CRM Communication Status": "MASTER",
	"CRM Service Level Agreement": "MASTER",
	"CRM Fields Layout": "MASTER",
	# Carries `user` and `private` — a person's own dashboard; the Settings panel of that name edits FCRM Settings.
	"CRM Dashboard": "MASTER",
	# Stock ships this with no System Manager row at all; the bucket restores one.
	"CRM Holiday List": "MASTER",
	"CRM Doctor": "MASTER",
	"CRM Hospital": "MASTER",
	"CRM City": "MASTER",
	"CRM State": "MASTER",
	# The platform's own shape. A grain axis is a Link target on every lead, so read stays for everyone.
	"CRM Vertical": "PLATFORM_READ",
	"CRM Group": "PLATFORM_READ",
	"CRM Program": "PLATFORM_READ",
	"CRM Grain": "PLATFORM_READ",
	# A manager owns their own org chart: they place and re-parent people. Derived from the bucket, never
	# a copy of its rows — the readers stay whatever PLATFORM_READ says they are.
	"CRM Sales Hierarchy": {**BUCKETS["PLATFORM_READ"], SALES_MANAGER: (1, 1, 1, 0)},
	# The SPA loads these through a list resource (`data/script.js`), so a rep's read is load-bearing.
	"CRM Form Script": "PLATFORM_READ",
	# Brand/General/Home Actions — platform config an admin owns; every session reads it on boot for branding.
	"FCRM Settings": "PLATFORM_READ",
	# `update_quick_filters` INSERTS the first row per doctype, so the manager editing them needs create.
	"CRM Global Settings": "MASTER",
	# A user's OWN extension row — TelephonySettings.vue creates it, and that panel has no manager gate.
	"CRM Telephony Agent": "OPERATIONAL",
	# Credentials and site wiring; every runtime reader goes through db.get_value, which asks nothing.
	"ERPNext CRM Settings": "PLATFORM",
	# Grain scoping. A manager sets it on every person they add, so it rides with the invite rather than
	# waiting on an administrator. Tier 0 and deliberately opened — see TIER0_REVIEWED.
	"User Permission": {**BUCKETS["PLATFORM"], SALES_MANAGER: (1, 1, 1, 1)},
	# VAPT P1 IDOR — edit/delete only your OWN comment; create/read run elsewhere.
	"Comment": {
		SYSTEM_MANAGER: (1, 1, 1, 1),
		"Website Manager": (1, 1, 1, 1),  # preserve the stock grant (Custom DocPerm overrides stock)
		ALL: (0, 1, 0, 1, 1),
	},
	# media_for gates on the CALL LOG, so the modal needs no role grant here.
	"CRM Call Media": "PLATFORM",
	# Frappe core, admin-only as shipped, and our own menus offer each to another role. SPA Settings shows Assignment Rules to a Sales Manager; helpdesk's ticket rotation is Agent Manager's.
	"Assignment Rule": {SYSTEM_MANAGER: (1, 1, 1, 1), SALES_MANAGER: (1, 1, 1, 1), AGENT_MANAGER: (1, 1, 1, 1)},
	# On the Automations / External Leads / Observability workspaces. A Call API node cannot publish until its Webhook exists, so that one is theirs to create; the rest are diagnostics.
	"Webhook": {SYSTEM_MANAGER: (1, 1, 1, 1), AUTOMATION_MANAGER: (1, 1, 1, 1)},
	"Webhook Request Log": {SYSTEM_MANAGER: (1, 1, 1, 1), AUTOMATION_MANAGER: (1, 0, 0, 0)},
	"Integration Request": {SYSTEM_MANAGER: (1, 1, 1, 1), AUTOMATION_MANAGER: (1, 0, 0, 0)},
	"Error Log": {SYSTEM_MANAGER: (1, 1, 1, 1), AUTOMATION_MANAGER: (1, 0, 0, 0)},
	# A rep's tool, not site wiring. DESK_USER keeps frappe's own any-staff read, which a rebuild would drop.
	"Email Template": {SYSTEM_MANAGER: (1, 1, 1, 1), SALES_MANAGER: (1, 1, 1, 1), DESK_USER: (1, 0, 0, 0)},
	# Code execution in the viewer's session — killed.
	"Custom HTML Block": "DENIED",
}

# Agent-only internal, no customer portal. Agent Manager is spelled out: helpdesk never grants it `Agent`.
_HELPDESK = {
	"HD Ticket": "HD_WORK",
	"HD Ticket Comment": "HD_WORK",
	"HD Ticket Activity": "HD_WORK",
	"HD Customer": "HD_WORK",
	"HD Article": "HD_WORK",
	"HD Article Category": "HD_WORK",
	"HD Article Feedback": "HD_WORK",
	"HD Ticket Type": "HD_CONFIG",
	"HD Ticket Priority": "HD_CONFIG",
	"HD Ticket Status": "HD_CONFIG",
	"HD Ticket Template": "HD_CONFIG",
	"HD Ticket Feedback Option": "HD_CONFIG",
	"HD Team": "HD_CONFIG",
	"HD Service Level Agreement": "HD_CONFIG",
	"HD Service Holiday List": "HD_CONFIG",
	"HD Saved Reply": "HD_CONFIG",
	"HD Field Layout": "HD_CONFIG",
	"HD Agent": "HD_CONFIG",
	"HD Agent Status": "HD_CONFIG",
	# A saved view is per-user state, so its owner deletes their own (same shape as CRM View Settings).
	"HD View": "HD_WORK",
	# An agent's own notification rows.
	"HD Notification": {SYSTEM_MANAGER: (1, 1, 1, 1), AGENT: (1, 1, 1, 0)},
	# An accepted invitation inserts a User with permissions ignored, so holding this row is holding account creation. helpdesk grants it to Agent Manager.
	"User Invitation": "PLATFORM",
	# Credentials, executable scripts and the search dictionaries.
	"HD Settings": "PLATFORM",
	"ERPNext HD Settings": "PLATFORM",
	"HD Form Script": "PLATFORM",
	"HD Stopword": "PLATFORM",
	"HD Synonyms": "PLATFORM",
	"HD Email Feedback": "PLATFORM",
	# frappe core's Website article, unrelated to the helpdesk KB (HD Article) and unused here.
	"Help Article": "DENIED",
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
	# What a reader touches: the page, and the space that lists it. Page content is read by db.get_value, which asks nothing.
	"Wiki Document": "WIKI_READ",
	"Wiki Space": "WIKI_READ",
	# Authoring. `create_overlay_revision` inserts with ignore_permissions, but the CR and its items insert checked.
	"Wiki Change Request": "WIKI",
	"Wiki Revision": "WIKI",
	"Wiki Revision Item": "WIKI",
	# Every access is `frappe.get_value` or an ignore_permissions insert, so a row here would be decoration.
	"Wiki Content Blob": "PLATFORM",
	"Wiki Merge Conflict": "PLATFORM",
	# Drops the stock All AND Guest write/create: an anonymous caller could create and edit ratings.
	"Wiki Feedback": {SYSTEM_MANAGER: (1, 1, 1, 1), WIKI_MANAGER: (1, 1, 1, 1), WIKI_USER: (0, 0, 1, 0)},
	# Site-wide config; head_html, javascript and the GitHub secrets sit at permlevel 1 (lockdown.FIELD_LEVELS).
	"Wiki Settings": {SYSTEM_MANAGER: (1, 1, 1, 1), WIKI_MANAGER: (1, 1, 0, 0)},
	# The v2 page flow, superseded by Wiki Document + Change Request; Wiki Page Patch shipped `All` rwcd.
	"Wiki Page": "DENIED",
	"Wiki Page Patch": "DENIED",
	"Wiki Page Revision": "DENIED",
	# The v2 importer and the GitHub mirror; neither is how this handbook is written.
	"Migrate To Wiki": "PLATFORM",
	"Wiki Git Sync Log": "PLATFORM",
	"Wiki GitHub Connection": "PLATFORM",
	"Wiki GitHub Webhook Log": "PLATFORM",
}

# Ours. Same lane as everything above: the JSON these carry is the install-time seed, this is the truth.
_TATVA = {
	# What an automation author configures.
	"CRM Intake Form": "AUTOMATION",
	"CRM Intake Settings": "AUTOMATION",
	"CRM Facebook App": "AUTOMATION",
	# crm-app doctypes, but the same job and the same workspace (External Leads) as the tatva ones above.
	"Lead Sync Source": "AUTOMATION",
	"Facebook Lead Form": "AUTOMATION",
	"Facebook Page": "AUTOMATION",
	"Failed Lead Sync Log": "AUTOMATION_LOG",
	"CRM Lead Import": "AUTOMATION",
	"CRM API Metric Settings": "AUTOMATION",
	"CRM Lead API Field": "AUTOMATION",
	"CRM Lead API Mapping": "AUTOMATION",
	"CRM Partner API Settings": "AUTOMATION",
	"CRM Cohort Pace Settings": "AUTOMATION",
	"CRM Contact Cap Settings": "AUTOMATION",
	"CRM Tatva Automation": "AUTOMATION",
	"CRM Workflow": "AUTOMATION",
	"CRM Workflow Journey": "AUTOMATION",
	"CRM Workflow Node": "AUTOMATION",
	"CRM Workflow Signal": "AUTOMATION",
	"CRM Campaign Document": "AUTOMATION",
	# What an automation author reads; the four logs carry the tiles their workspaces render.
	"CRM Workflow Step Log": "AUTOMATION_LOG",
	"CRM Partner API Idempotency": "AUTOMATION_LOG",
	"CRM API Metric": "AUTOMATION_LOG",
	"CRM API Request Log": "AUTOMATION_LOG",
	"CRM Bulk Job": "AUTOMATION_LOG",
	"CRM Bulk Job Result": "AUTOMATION_LOG",
	# Credentials, keys and site wiring.
	"CRM Notification Preference": "PLATFORM",
	"CRM Notification Settings": "PLATFORM",
	"CRM Push Settings": "PLATFORM",
	"CRM Push Subscription": "PLATFORM",
	"CRM Payment": "PLATFORM",
	"CRM Maps Settings": "PLATFORM",
	# Nine rows naming the lead's sections, not wiring — admin-only refused every intake-form author who opened one.
	"CRM Lead Section": "PLATFORM_READ",
	"CRM Search Alias": "PLATFORM",
	"CRM Lead Field Restriction": "PLATFORM",
	# A rep BUILDS these; PLATFORM was wrong. `smartview/permissions.py` owns who sees which — this row only opens the door.
	"CRM Smart View": {
		SYSTEM_MANAGER: (1, 1, 1, 1),
		SALES_MANAGER: (1, 1, 1, 1),
		SALES_USER: (1, 1, 1, 1),
		AUTOMATION_MANAGER: (1, 1, 1, 1),
	},
	"CRM Azure Storage Settings": "PLATFORM",
	"CRM File Scan Log": "PLATFORM",
	"CRM File Screening Settings": "PLATFORM",
	"CRM Transcription Account": "PLATFORM",
	"CRM Dashboard Chart": "PLATFORM",
	"CRM Derived Field": "PLATFORM",
	"CRM Trusted Fetch Host": "PLATFORM",
	"CRM Telephony Account": "PLATFORM",
	"CRM Telephony Routing": "PLATFORM",
	"CRM Telephony Settings": "PLATFORM",
	"CRM AI Voice Account": "PLATFORM",
	# Append-only or derived: read-only even for an admin, so PLATFORM would WIDEN them.
	"CRM Timeline Event": {SYSTEM_MANAGER: (1, 0, 0, 0)},
	"CRM Control Tower": {SYSTEM_MANAGER: (1, 0, 0, 0)},
	"CRM Workflow Version": {SYSTEM_MANAGER: (1, 0, 0, 0)},
	# WhatsApp is a capability, granted by its own roles — same shape as the upstream rows above.
	"CRM WhatsApp Routing": {SYSTEM_MANAGER: (1, 1, 1, 1), WHATSAPP_ADMIN: (1, 1, 1, 1)},
	"CRM WhatsApp Settings": {SYSTEM_MANAGER: (1, 1, 1, 1), WHATSAPP_ADMIN: (1, 1, 1, 1)},
	# The compliance trail; Insights enforces DocPerm, so the manager's read is load-bearing.
	"CRM Visit Audit": {SYSTEM_MANAGER: (1, 1, 1, 1), SALES_MANAGER: (1, 0, 0, 0)},
}

# Internal staff training. Moderator grants nothing here — it exists only as lms's own staff test (see BASELINE_ROLE_TRIMS).
_LMS = {
	"Course Chapter": {LMS_STUDENT: (1, 0, 0, 0), COURSE_CREATOR: (1, 1, 1, 1), SYSTEM_MANAGER: (1, 1, 1, 1)},
	"Course Evaluator": {
		COURSE_CREATOR: (1, 1, 1, 1),
		BATCH_EVALUATOR: (1, 1, 1, 1),
		SYSTEM_MANAGER: (1, 1, 1, 1)
	},
	"Course Lesson": {COURSE_CREATOR: (1, 1, 1, 1, 1), SYSTEM_MANAGER: (1, 1, 1, 1)},
	"Function": "DENIED",
	"Industry": "DENIED",
	"Job Opportunity": "DENIED",
	"LMS Assignment": {
		LMS_STUDENT: (1, 0, 0, 0),
		COURSE_CREATOR: (1, 1, 1, 1),
		BATCH_EVALUATOR: (1, 1, 1, 1),
		SYSTEM_MANAGER: (1, 1, 1, 1)
	},
	"LMS Assignment Submission": {
		LMS_STUDENT: (1, 1, 1, 0, 1),
		COURSE_CREATOR: (1, 1, 1, 1),
		BATCH_EVALUATOR: (1, 1, 1, 1),
		SYSTEM_MANAGER: (1, 1, 1, 1)
	},
	"LMS Badge": "PLATFORM",
	"LMS Badge Assignment": {
		LMS_STUDENT: (1, 0, 0, 0, 1),
		COURSE_CREATOR: (1, 1, 1, 1),
		BATCH_EVALUATOR: (1, 1, 1, 1),
		SYSTEM_MANAGER: (1, 1, 1, 1)
	},
	"LMS Batch": {LMS_STUDENT: (1, 0, 0, 0), BATCH_EVALUATOR: (1, 1, 1, 1), SYSTEM_MANAGER: (1, 1, 1, 1)},
	"LMS Batch Enrollment": {
		LMS_STUDENT: (1, 0, 0, 0, 1),
		BATCH_EVALUATOR: (1, 1, 1, 1),
		SYSTEM_MANAGER: (1, 1, 1, 1)
	},
	"LMS Batch Feedback": {
		LMS_STUDENT: (1, 1, 1, 0, 1),
		BATCH_EVALUATOR: (1, 1, 1, 1),
		SYSTEM_MANAGER: (1, 1, 1, 1)
	},
	# The taxonomy is site shape, edited from the Settings panel — an author picks a category, they do not coin one.
	"LMS Category": {
		COURSE_CREATOR: (1, 0, 0, 0),
		BATCH_EVALUATOR: (1, 0, 0, 0),
		SYSTEM_MANAGER: (1, 1, 1, 1)
	},
	"LMS Certificate": {
		LMS_STUDENT: (1, 0, 0, 0),
		COURSE_CREATOR: (1, 1, 1, 1),
		BATCH_EVALUATOR: (1, 1, 1, 1),
		SYSTEM_MANAGER: (1, 1, 1, 1)
	},
	"LMS Certificate Evaluation": {BATCH_EVALUATOR: (1, 1, 1, 1), SYSTEM_MANAGER: (1, 1, 1, 1)},
	"LMS Certificate Request": {
		LMS_STUDENT: (1, 0, 0, 0, 1),
		COURSE_CREATOR: (1, 1, 1, 1),
		BATCH_EVALUATOR: (1, 1, 1, 1),
		SYSTEM_MANAGER: (1, 1, 1, 1)
	},
	"LMS Coupon": "DENIED",
	"LMS Course": {COURSE_CREATOR: (1, 1, 1, 1), SYSTEM_MANAGER: (1, 1, 1, 1)},
	"LMS Course Interest": "DENIED",
	"LMS Course Mentor Mapping": "DENIED",
	"LMS Course Progress": {
		LMS_STUDENT: (1, 0, 0, 0, 1),
		COURSE_CREATOR: (1, 1, 1, 1),
		SYSTEM_MANAGER: (1, 1, 1, 1)
	},
	"LMS Course Review": {
		COURSE_CREATOR: (1, 1, 1, 1),
		BATCH_EVALUATOR: (1, 1, 1, 1),
		SYSTEM_MANAGER: (1, 1, 1, 1)
	},
	"LMS Enrollment": {
		LMS_STUDENT: (1, 0, 0, 0, 1),
		COURSE_CREATOR: (1, 1, 1, 1),
		BATCH_EVALUATOR: (1, 1, 1, 0),
		SYSTEM_MANAGER: (1, 1, 1, 1)
	},
	"LMS Google Meet Settings": {BATCH_EVALUATOR: (1, 1, 1, 1, 1), SYSTEM_MANAGER: (1, 1, 1, 1)},
	"LMS Job Application": "DENIED",
	"LMS Lesson Note": {
		LMS_STUDENT: (1, 1, 1, 1, 1),
		COURSE_CREATOR: (1, 1, 1, 1),
		SYSTEM_MANAGER: (1, 1, 1, 1)
	},
	"LMS Live Class": {
		LMS_STUDENT: (1, 0, 0, 0),
		BATCH_EVALUATOR: (1, 1, 1, 1),
		SYSTEM_MANAGER: (1, 1, 1, 1)
	},
	"LMS Live Class Participant": {BATCH_EVALUATOR: (1, 1, 1, 1), SYSTEM_MANAGER: (1, 1, 1, 1)},
	"LMS Payment": "DENIED",
	"LMS Program": {LMS_STUDENT: (1, 0, 0, 0), COURSE_CREATOR: (1, 1, 1, 1), SYSTEM_MANAGER: (1, 1, 1, 1)},
	"LMS Programming Exercise": {
		LMS_STUDENT: (1, 0, 0, 0),
		COURSE_CREATOR: (1, 1, 1, 1),
		BATCH_EVALUATOR: (1, 1, 1, 1),
		SYSTEM_MANAGER: (1, 1, 1, 1)
	},
	"LMS Programming Exercise Submission": {
		LMS_STUDENT: (1, 1, 1, 0, 1),
		COURSE_CREATOR: (1, 1, 1, 1),
		BATCH_EVALUATOR: (1, 1, 1, 1),
		SYSTEM_MANAGER: (1, 1, 1, 1)
	},
	"LMS Question": {COURSE_CREATOR: (1, 1, 1, 1), SYSTEM_MANAGER: (1, 1, 1, 1)},
	"LMS Quiz": {
		LMS_STUDENT: (1, 0, 0, 0),
		COURSE_CREATOR: (1, 1, 1, 1),
		BATCH_EVALUATOR: (1, 1, 1, 1),
		SYSTEM_MANAGER: (1, 1, 1, 1)
	},
	"LMS Quiz Submission": {LMS_STUDENT: (1, 0, 0, 0, 1), SYSTEM_MANAGER: (1, 1, 1, 1)},
	"LMS Settings": "PLATFORM",
	"LMS Source": {LMS_STUDENT: (1, 0, 0, 0), SYSTEM_MANAGER: (1, 1, 1, 1)},
	"LMS Timetable Template": "PLATFORM",
	"LMS Video Watch Duration": {
		LMS_STUDENT: (1, 1, 1, 0, 1),
		COURSE_CREATOR: (1, 1, 1, 1),
		SYSTEM_MANAGER: (1, 1, 1, 1)
	},
	"LMS Zoom Settings": {BATCH_EVALUATOR: (1, 1, 1, 1, 1), SYSTEM_MANAGER: (1, 1, 1, 1)},
	"User Skill": "DENIED",
	"Zoom Settings": "PLATFORM",
}

# BI over the site DB itself: no read on a doctype and its table resolves to `filter(False)`, not an error.
_INSIGHTS = {
	# Sharing is gated on the `share` ptype (the 7th element), so a consumer can never re-share what they read.
	"Insights Workbook": {
		SYSTEM_MANAGER: (1, 1, 1, 1, 0, 0, 1),
		INSIGHTS_ADMIN: (1, 1, 1, 1, 0, 0, 1),
		INSIGHTS_USER: (1, 0, 0, 0),
	},
	# `update_access` writes `is_public` with db_set, so the ONE gate on publishing is this `share` right.
	"Insights Dashboard v3": {
		SYSTEM_MANAGER: (1, 1, 1, 1, 0, 0, 1),
		INSIGHTS_ADMIN: (1, 1, 1, 1, 0, 0, 1),
		INSIGHTS_USER: (1, 0, 0, 0),
	},
	# What a consumer opens; the team's resource permissions narrow WHICH of these rows they get.
	"Insights Chart v3": "INSIGHTS_READ",
	"Insights Query v3": "INSIGHTS_READ",
	"Insights Folder": "INSIGHTS_READ",
	"Insights Table v3": "INSIGHTS_READ",
	"Insights Table Link v3": "INSIGHTS_READ",
	"Insights Data Source v3": "INSIGHTS_READ",
	# The team roster itself: an author curates it, a consumer reads their own membership.
	"Insights Team": "INSIGHTS_READ",
	# Authoring-only; a consumer never schedules an alert.
	"Insights Alert": "INSIGHTS",
	# Append-only trail of what ran.
	"Insights Query Execution Log": {SYSTEM_MANAGER: (1, 1, 1, 1), INSIGHTS_ADMIN: (1, 0, 0, 0)},
	# Every page loads this Single (AppSidebar reads `enable_data_store`), so a consumer's read is load-bearing.
	"Insights Settings": {
		SYSTEM_MANAGER: (1, 1, 1, 1),
		INSIGHTS_ADMIN: (1, 1, 0, 0),
		INSIGHTS_USER: (1, 0, 0, 0),
	},
	# Loading a spreadsheet into a new table is a platform act, not an authoring one.
	"Insights Table Import": "PLATFORM",
	"Insights Table Import Job": "PLATFORM",
	"Insights Table Import Log": "PLATFORM",
	# Accepting one mints a login, so `insights_invitation` refuses an address that does not already sign in here.
	"Insights User Invitation": "PLATFORM",
	# v2. The v3 SPA reads none of these, and denying them closes the old `public_key` surface in api/public.py.
	"Insights Query": "DENIED",
	"Insights Chart": "DENIED",
	"Insights Dashboard": "DENIED",
	"Insights Table": "DENIED",
	"Insights Data Source": "DENIED",
	"Insights Notebook": "DENIED",
	"Insights Notebook Page": "DENIED",
	"Insights Query Chart": "DENIED",
	"Insights Query Reference": "DENIED",
	"Insights Query Result": "DENIED",
}

OPEN = {**_CRM_CORE, **_TATVA, **_HELPDESK, **_WHATSAPP, **_WIKI, **_INSIGHTS, **_LMS, **_PLATFORM_USER}

# Tail rights that ride any role which reads: `email` for communication.email.make, `export` for can_export.
EXTRA_PTYPES = {
	# Granted by hand on prod 2026-08-21 and declared here so a rebuild keeps it — an undeclared right is one the next rebuild silently drops.
	"CRM Visit Audit": ("import",),
	"CRM Lead": ("email", "export"),
	"CRM Deal": ("email", "export"),
	"CRM Task": ("export",),
	"CRM Call Log": ("export",),
	"FCRM Note": ("export",),
	"CRM Organization": ("export",),
	"Contact": ("export",),
}


def extra_ptypes_for(doctype):
	"""The tail rights this doctype grants on top of r/w/c/d — empty for everything not named above."""
	return EXTRA_PTYPES.get(doctype, ())


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
