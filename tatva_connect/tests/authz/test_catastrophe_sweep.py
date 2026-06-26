# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Tier-1 catastrophe sweeps (TESTS.md §4) — blanket DENY invariants over EVERY doctype.

These are cheap, exhaustive, programmatic: one `has_permission` call per (doctype, role, action).
The oracle is `native_doctype_capability` (doc=None) — sanctioned here because a doctype-level DENY
is the strongest possible verdict (no false negative). Each sweep COLLECTS every violation and fails
ONCE, listing each offending (doctype, role, ptype) so a single failure names every leak.

Sweep 1/2 (Guest, no-role) need only `has_permission(user=...)` which works without seeded leads, so
they don't require generator.seed(). Sweeps 3/4 reference the junk_crossapp persona's user, so
setUpClass calls generator.seed() to provision the roster. seed() fails loud if masters aren't
seeded — acceptable per the constitution (no auto-seeded masters); the class simply errors out.
"""
import frappe

from tatva_connect.tests.authz import generator, roster
from tatva_connect.tests.authz.base import AuthzTestCase
from tatva_connect.tests.authz.oracle import native_doctype_capability

# Roles meant to write broadly — the only ones exempt from the outside-remit deny sweep.
PRIVILEGED = {"System Manager", "Administrator", "Sales Manager"}

# The catastrophe actions: a positive grant on any of these to the wrong principal is a breach.
WRITE_ACTIONS = ("write", "create", "delete")

# Sensitive CRM doctypes that hold patient / lead / comms data — the crown jewels.
SENSITIVE_CRM = [
	"CRM Lead", "CRM Deal", "CRM Task", "FCRM Note",
	"CRM Call Log", "Contact", "WhatsApp Message",
]


def _non_table_doctypes():
	"""Every non-child doctype in the live DB (~392) — the full catastrophe surface."""
	return frappe.get_all("DocType", filters={"istable": 0}, pluck="name")


class TestCatastropheSweep(AuthzTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()  # comms-off gate + class rollback floor
		# Provisions the full roster (incl. junk_crossapp = Purchase Master Manager). Fails loud if
		# masters aren't seeded — that's the accepted precondition for sweeps 3/4.
		generator.seed(commit=False)
		cls.doctypes = _non_table_doctypes()

	# ---------- 1: Guest can NEVER write any doctype ----------
	def test_guest_cannot_write_any_doctype(self):
		violations = [
			(dt, ptype)
			for dt in self.doctypes
			for ptype in WRITE_ACTIONS
			if native_doctype_capability("Guest", dt, ptype)
		]
		self.assertFalse(
			violations,
			"CATASTROPHE: Guest can write {0} doctype/action pair(s): {1}".format(
				len(violations), violations),
		)

	# ---------- 2: a no-role authenticated user can NEVER write any doctype ----------
	def test_no_role_user_cannot_write_any_doctype(self):
		user = roster.email("no_role")
		violations = [
			(dt, ptype)
			for dt in self.doctypes
			for ptype in WRITE_ACTIONS
			if native_doctype_capability(user, dt, ptype)
		]
		self.assertFalse(
			violations,
			"CATASTROPHE: no-role user {0} can write {1} doctype/action pair(s): {2}".format(
				user, len(violations), violations),
		)

	# ---------- 3: cross-app junk role denied READ on sensitive CRM doctypes ----------
	# Re-establishes the VAPT regression (Purchase Master Manager reading Contact, etc.).
	def test_cross_app_junk_role_denied_on_sensitive_crm_doctypes(self):
		user = roster.email("junk_crossapp")  # Purchase Master Manager
		violations = [
			dt for dt in SENSITIVE_CRM
			if native_doctype_capability(user, dt, "read")
		]
		self.assertFalse(
			violations,
			"CROSS-APP LEAK: junk role (Purchase Master Manager) {0} can READ sensitive CRM "
			"doctype(s): {1}".format(user, violations),
		)

	# ---------- 4: every non-privileged ROLE denied write/create/delete outside its remit ----------
	# Asserts only clear catastrophe cases (write/create/delete) on the sensitive CRM doctypes.
	# Borderline READ grants are LOGGED (informational), never failed — a role may legitimately read.
	def test_every_nonprivileged_role_denied_write_outside_remit(self):
		roles = [r for r in frappe.get_all("Role", pluck="name") if r not in PRIVILEGED]
		violations = []
		read_grants = []
		for role in roles:
			# has_permission takes a user, not a role; impersonate a transient user holding ONLY
			# this role so the verdict is exactly this role's ceiling.
			probe = self._probe_user(role)
			for dt in SENSITIVE_CRM:
				for ptype in WRITE_ACTIONS:
					if native_doctype_capability(probe, dt, ptype):
						violations.append((role, dt, ptype))
				if native_doctype_capability(probe, dt, "read"):
					read_grants.append((role, dt))

		if read_grants:
			frappe.logger().info(
				"authz catastrophe sweep — informational READ grants on sensitive CRM "
				"doctypes (not a failure): {0}".format(read_grants)
			)
		self.assertFalse(
			violations,
			"CATASTROPHE: non-privileged role(s) can write sensitive CRM doctypes outside their "
			"remit — {0} pair(s): {1}".format(len(violations), violations),
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
