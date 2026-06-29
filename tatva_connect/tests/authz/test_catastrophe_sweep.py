# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Tier-1 catastrophe sweeps (TESTS.md §4) — blanket DENY invariants over EVERY doctype.

Cheap, exhaustive, programmatic: one `has_permission` call per (doctype, role, action). The oracle is
`native_doctype_capability` (doc=None) — sanctioned here because a doctype-level DENY is the strongest
possible verdict (no false negative). Each sweep COLLECTS every violation and fails ONCE, naming each
offending (doctype, role, ptype) so a single failure lists every leak.

The headline question this file answers: **can an unauthenticated (Guest) or no-role user touch OUR
data or take over the instance?** Two complementary guarantees:

  1. Hard, DYNAMIC guarantee (`test_our_data_untouchable_*`, `test_admin_takeover_denied_*`): Guest and
     no-role can reach NONE of our doctypes, NONE of the sensitive CRM crown jewels, and cannot write
     any system-takeover doctype. Enumerated live, so a NEW doctype is covered automatically — no
     allowlist to forget. This is the load-bearing check.

  2. Drift guard (`test_guest_writes_only_reviewed_stock`, `..._no_role_...`): Frappe and the stock apps
     (Wiki/Helpdesk/LMS) legitimately grant a Guest/authenticated user some writes — anonymous wiki
     feedback, your own ToDo/File/Tag, a helpdesk ticket. We don't fork those apps (constitution A.1),
     so we ACCEPT those grants via a REVIEWED allowlist (audit 2026-06-29) and assert nothing NEW slips
     in. The allowlist is independently asserted to contain none of our/crown/admin doctypes, so it can
     never be used to launder a real leak.

Sweeps that only call `has_permission(user=...)` work without seeded leads; the junk_crossapp + lead-
role sweeps reference the roster, so setUpClass seeds it (fails loud if masters are unseeded — the
accepted precondition; the constitution forbids auto-seeding masters).
"""
import frappe

from tatva_connect.tests.authz import generator, roster
from tatva_connect.tests.authz.base import AuthzTestCase
from tatva_connect.tests.authz.oracle import native_doctype_capability

# Roles meant to write broadly — exempt from the outside-remit deny sweep.
PRIVILEGED = {"System Manager", "Administrator", "Sales Manager"}

# Roles that LEGITIMATELY hold doctype-level write on the sensitive CRM doctypes — their job is working
# leads/tasks. Doctype capability is EXPECTED for them; the real guard is ROW-LEVEL grain scoping,
# proven by tests/tasks, tests/notes, tests/telephony, and tests/authz/test_registry_cases (A1/A2). So
# the outside-remit sweep EXEMPTS them and catches only a role with NO lead business that nonetheless
# gained CRM write (e.g. a cross-app role). Reviewed 2026-06-29 against the live grant set.
LEAD_WORKING_ROLES = {"Sales User", "Niva Lead Creator"}

WRITE_ACTIONS = ("write", "create", "delete")
ALL_ACTIONS = ("read", "write", "create", "delete")

# Sensitive CRM doctypes that hold patient / lead / comms data — the crown jewels.
SENSITIVE_CRM = [
	"CRM Lead", "CRM Deal", "CRM Task", "FCRM Note",
	"CRM Call Log", "Contact", "WhatsApp Message",
]

# System-takeover doctypes: a write here = owning the instance (new roles, perms, server scripts).
# `User` is handled separately — Frappe lets a user read/write their OWN profile (row-scoped), which is
# NOT takeover; create/delete User IS, and is asserted denied below.
ADMIN_STRUCTURAL = [
	"Role", "DocType", "DocPerm", "Custom DocPerm", "System Settings", "Server Script",
	"Custom Field", "Property Setter", "Workflow", "Role Profile", "Module Profile",
]

# --- REVIEWED stock/core write grants (audit 2026-06-29) -----------------------------------------
# Doctypes a GUEST (unauthenticated) may write — stock APP features we don't fork. Not ours, not a
# crown jewel, not admin (asserted in test_guest_writes_only_reviewed_stock).
STOCK_GUEST_WRITABLE = {
	"HD View",        # Helpdesk: a saved list view
	"Wiki Feedback",  # Wiki: anonymous page feedback (the app's public feature)
}

# Doctypes a NO-ROLE authenticated user may write. Two safe classes:
#   (a) Frappe-personal, owner-scoped — every logged-in user owns their own row.
#   (b) stock app (Helpdesk / Wiki / LMS) doctypes we don't fork.
# Neither is ours / a crown jewel / a structural-admin doctype (asserted below).
STOCK_NOROLE_WRITABLE = {
	# (a) Frappe-personal, owner-scoped
	"Address", "Custom HTML Block", "Dashboard Settings", "Desktop Icon", "Desktop Layout",
	"Document Follow", "File", "Google Calendar", "Google Contacts", "Kanban Board", "List Filter",
	"Note", "Notification Settings", "Reminder", "Tag", "Tag Link", "ToDo", "User", "Workspace",
	"Workspace Sidebar", "Workflow Action",
	# (b) stock app (Helpdesk / Wiki / LMS) — not forked, not our data
	"Discussion Reply", "Discussion Topic", "HD Article Feedback", "HD Ticket", "HD View",
	"Job Opportunity", "LMS Assignment Submission", "LMS Badge Assignment", "LMS Batch Enrollment",
	"LMS Batch Feedback", "LMS Certificate Request", "LMS Course Progress", "LMS Course Review",
	"LMS Enrollment", "LMS Job Application", "LMS Lesson Note", "LMS Programming Exercise Submission",
	"LMS Video Watch Duration", "Wiki Change Request", "Wiki Feedback", "Wiki Page Patch",
	"Wiki Revision", "Wiki Revision Item",
}


def _non_table_doctypes():
	"""Every non-child doctype in the live DB (~392) — the full catastrophe surface."""
	return frappe.get_all("DocType", filters={"istable": 0}, pluck="name")


def _our_doctypes():
	"""Every tatva_connect doctype in the live DB (the doctype's module belongs to our app)."""
	out = []
	for d in frappe.get_all("DocType", filters={"istable": 0}, fields=["name", "module"]):
		if frappe.db.get_value("Module Def", d.module, "app_name") == "tatva_connect":
			out.append(d.name)
	return out


class TestCatastropheSweep(AuthzTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()  # comms-off gate + class rollback floor
		generator.seed(commit=False)  # roster incl. no_role + junk_crossapp (Purchase Master Manager)
		cls.doctypes = _non_table_doctypes()
		cls.ours = _our_doctypes()

	# ---------- 1: OUR data + crown jewels are untouchable by Guest / no-role (read OR write) -------
	def test_our_data_untouchable_by_guest_and_norole(self):
		"""The load-bearing guarantee: an unauthenticated (Guest) or no-role user can do NOTHING —
		not even read — to any tatva_connect doctype or any sensitive CRM crown jewel. Dynamic: new
		doctypes are covered automatically."""
		protected = sorted(set(self.ours) | set(SENSITIVE_CRM))
		norole = roster.email("no_role")
		leaks = [
			(who, dt, p)
			for who, user in (("Guest", "Guest"), ("no_role", norole))
			for dt in protected
			for p in ALL_ACTIONS
			if native_doctype_capability(user, dt, p)
		]
		self.assertFalse(
			leaks,
			"CATASTROPHE: Guest/no-role can reach protected data — {0} grant(s): {1}".format(
				len(leaks), leaks),
		)

	# ---------- 2: system-takeover doctypes are denied to Guest / no-role ---------------------------
	def test_admin_takeover_denied_to_guest_and_norole(self):
		"""Guest and no-role cannot write any structural-admin doctype (Role/DocType/DocPerm/Server
		Script/…) nor create/delete a User — the 'take over the instance' path. (A no-role user may
		read+write its OWN User row — Frappe row-scopes that — so User write is NOT asserted here;
		create/delete User IS.)"""
		norole = roster.email("no_role")
		leaks = []
		for who, user in (("Guest", "Guest"), ("no_role", norole)):
			for dt in ADMIN_STRUCTURAL:
				for p in ALL_ACTIONS:
					if native_doctype_capability(user, dt, p):
						leaks.append((who, dt, p))
			for p in ("create", "delete"):
				if native_doctype_capability(user, "User", p):
					leaks.append((who, "User", p))
		self.assertFalse(
			leaks,
			"CATASTROPHE: Guest/no-role can reach a system-takeover doctype — {0}: {1}".format(
				len(leaks), leaks),
		)

	# ---------- 3: Guest may write ONLY the reviewed stock allowlist --------------------------------
	def test_guest_writes_only_reviewed_stock(self):
		writable = {
			dt for dt in self.doctypes for p in WRITE_ACTIONS
			if native_doctype_capability("Guest", dt, p)
		}
		self._assert_allowlist_is_clean(STOCK_GUEST_WRITABLE, "Guest")
		unreviewed = sorted(writable - STOCK_GUEST_WRITABLE)
		self.assertFalse(
			unreviewed,
			"CATASTROPHE: Guest can write UNREVIEWED doctype(s) — review each, then add to "
			"STOCK_GUEST_WRITABLE with a reason (or lock it down): {0}".format(unreviewed),
		)

	# ---------- 4: a no-role user may write ONLY the reviewed stock allowlist -----------------------
	def test_no_role_writes_only_reviewed_stock(self):
		user = roster.email("no_role")
		writable = {
			dt for dt in self.doctypes for p in WRITE_ACTIONS
			if native_doctype_capability(user, dt, p)
		}
		self._assert_allowlist_is_clean(STOCK_NOROLE_WRITABLE, "no_role")
		unreviewed = sorted(writable - STOCK_NOROLE_WRITABLE)
		self.assertFalse(
			unreviewed,
			"CATASTROPHE: no-role user can write UNREVIEWED doctype(s) — review each, then add to "
			"STOCK_NOROLE_WRITABLE with a reason (or lock it down): {0}".format(unreviewed),
		)

	def _assert_allowlist_is_clean(self, allowlist, who):
		"""A reviewed stock allowlist can NEVER contain one of our doctypes, a crown jewel, or a
		structural-admin doctype — else it could launder a real leak past the sweep."""
		forbidden = (set(self.ours) | set(SENSITIVE_CRM) | set(ADMIN_STRUCTURAL)) & allowlist
		self.assertFalse(
			forbidden,
			"the {0} write-allowlist contains protected doctype(s) {1} — a reviewed allowlist must "
			"never bless our data / a crown jewel / an admin doctype".format(who, sorted(forbidden)),
		)

	# ---------- 5: cross-app junk role denied READ on sensitive CRM doctypes ------------------------
	# Re-establishes the VAPT regression (Purchase Master Manager reading Contact, etc.).
	def test_cross_app_junk_role_denied_on_sensitive_crm_doctypes(self):
		user = roster.email("junk_crossapp")  # Purchase Master Manager
		violations = [dt for dt in SENSITIVE_CRM if native_doctype_capability(user, dt, "read")]
		self.assertFalse(
			violations,
			"CROSS-APP LEAK: junk role (Purchase Master Manager) {0} can READ sensitive CRM "
			"doctype(s): {1}".format(user, violations),
		)

	# ---------- 6: a role with NO lead business cannot write sensitive CRM --------------------------
	def test_nonprivileged_nonlead_role_denied_write_on_sensitive_crm(self):
		"""Every role that is neither privileged NOR a legitimate lead-working role (LEAD_WORKING_ROLES)
		must have NO write/create/delete capability on the sensitive CRM doctypes. The lead-working
		roles are exempt because doctype-write IS their remit — their leak risk is ROW-level and is
		covered by the grain scope tests, not this doctype-capability sweep."""
		exempt = PRIVILEGED | LEAD_WORKING_ROLES
		roles = [r for r in frappe.get_all("Role", pluck="name") if r not in exempt]
		violations = []
		for role in roles:
			probe = self._probe_user(role)
			for dt in SENSITIVE_CRM:
				for ptype in WRITE_ACTIONS:
					if native_doctype_capability(probe, dt, ptype):
						violations.append((role, dt, ptype))
		self.assertFalse(
			violations,
			"CATASTROPHE: role(s) with no lead business can write sensitive CRM doctypes — {0} "
			"pair(s): {1}".format(len(violations), violations),
		)

	# ---------- helper ----------
	def _probe_user(self, role):
		"""A throwaway System User holding ONLY `role` — its has_permission verdict is that role's
		ceiling. Created with ignore_permissions; the class rollback unwinds it (no commit)."""
		email = "authz.probe.{0}@example.test".format(frappe.scrub(role))
		if not frappe.db.exists("User", email):
			frappe.get_doc({
				"doctype": "User", "email": email, "first_name": "probe-{0}".format(role),
				"user_type": "System User", "send_welcome_email": 0,
				"roles": [{"role": role}],
			}).insert(ignore_permissions=True)
		return email
