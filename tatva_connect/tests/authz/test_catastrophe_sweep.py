# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A Guest or no-role user cannot touch our data or take over the instance, across every doctype.
Stock-app writes they legitimately hold are pinned to a reviewed allowlist, so nothing new slips in."""
import frappe

from tatva_connect.tests.authz import generator, roster
from tatva_connect.tests.authz.base import AuthzTestCase
from tatva_connect.tests.authz.oracle import native_doctype_capability

# Roles meant to write broadly — exempt from the outside-remit deny sweep.
PRIVILEGED = {"System Manager", "Administrator", "Sales Manager"}

# Roles whose job is working leads; their real guard is row-level grain scope, tested elsewhere.
LEAD_WORKING_ROLES = {"Sales User", "Niva Lead Creator"}

WRITE_ACTIONS = ("write", "create", "delete")
ALL_ACTIONS = ("read", "write", "create", "delete")

# CRM doctypes that hold patient, lead or comms data.
SENSITIVE_CRM = [
	"CRM Lead", "CRM Deal", "CRM Task", "FCRM Note",
	"CRM Call Log", "Contact", "WhatsApp Message",
]

# A write here owns the instance; `User` is checked apart, since a user may edit their own row.
ADMIN_STRUCTURAL = [
	"Role", "DocType", "DocPerm", "Custom DocPerm", "System Settings", "Server Script",
	"Custom Field", "Property Setter", "Workflow", "Role Profile", "Module Profile",
]

# Stored values run in a browser or on the server, so an owner-scoped write is never safe here.
EXECUTES_CODE = [
	"Custom HTML Block", "Client Script", "Server Script", "Website Script", "Website Theme",
	"Web Page", "Web Form", "Print Format", "Report",
]

# Stock-app doctypes a Guest may write; none is ours, sensitive or admin.
STOCK_GUEST_WRITABLE = {
	"HD View",        # Helpdesk: a saved list view
	"Wiki Feedback",  # Wiki: anonymous page feedback (the app's public feature)
}

# Doctypes a no-role user may write: Frappe-personal owner-scoped rows, or stock Helpdesk/Wiki/LMS.
STOCK_NOROLE_WRITABLE = {
	# (a) Frappe-personal, owner-scoped
	"Address", "Dashboard Settings", "Desktop Icon", "Desktop Layout",
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
	"""Every non-child doctype in the live DB."""
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
		"""A Guest or no-role user cannot even read any tatva_connect or sensitive CRM doctype.
		Doctypes are listed live, so a new one is covered automatically."""
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
			f"CATASTROPHE: Guest/no-role can reach protected data — {len(leaks)} grant(s): {leaks}",
		)

	# ---------- 2: system-takeover doctypes are denied to Guest / no-role ---------------------------
	def test_admin_takeover_denied_to_guest_and_norole(self):
		"""Guest and no-role cannot touch any structural-admin doctype, nor create or delete a User.
		User write is not asserted, since Frappe lets a user edit their own row."""
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
			f"CATASTROPHE: Guest/no-role can reach a system-takeover doctype — {len(leaks)}: {leaks}",
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
			f"STOCK_GUEST_WRITABLE with a reason (or lock it down): {unreviewed}",
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
			f"STOCK_NOROLE_WRITABLE with a reason (or lock it down): {unreviewed}",
		)

	def _assert_allowlist_is_clean(self, allowlist, who):
		"""A stock allowlist never holds our, sensitive, admin or code-executing doctypes.
		Otherwise it could wave a real leak past the sweep."""
		forbidden = (
			set(self.ours) | set(SENSITIVE_CRM) | set(ADMIN_STRUCTURAL) | set(EXECUTES_CODE)
		) & allowlist
		self.assertFalse(
			forbidden,
			f"the {who} write-allowlist contains protected doctype(s) {sorted(forbidden)} — a reviewed allowlist must "
			"never bless our data / a crown jewel / an admin doctype",
		)

	# ---------- 5: cross-app junk role denied READ on sensitive CRM doctypes ------------------------
	def test_cross_app_junk_role_denied_on_sensitive_crm_doctypes(self):
		user = roster.email("junk_crossapp")  # Purchase Master Manager
		violations = [dt for dt in SENSITIVE_CRM if native_doctype_capability(user, dt, "read")]
		self.assertFalse(
			violations,
			f"CROSS-APP LEAK: junk role (Purchase Master Manager) {user} can READ sensitive CRM "
			f"doctype(s): {violations}",
		)

	# ---------- 6: a role with NO lead business cannot write sensitive CRM --------------------------
	def test_nonprivileged_nonlead_role_denied_write_on_sensitive_crm(self):
		"""A role that is neither privileged nor lead-working cannot write any sensitive CRM doctype."""
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
			f"CATASTROPHE: role(s) with no lead business can write sensitive CRM doctypes — {len(violations)} "
			f"pair(s): {violations}",
		)

	# ---------- helper ----------
	def _probe_user(self, role):
		"""A throwaway System User holding only `role`, so its verdict is that role's ceiling."""
		email = f"authz.probe.{frappe.scrub(role)}@example.test"
		if not frappe.db.exists("User", email):
			frappe.get_doc({
				"doctype": "User", "email": email, "first_name": f"probe-{role}",
				"user_type": "System User", "send_welcome_email": 0,
				"roles": [{"role": role}],
			}).insert(ignore_permissions=True)
		return email
