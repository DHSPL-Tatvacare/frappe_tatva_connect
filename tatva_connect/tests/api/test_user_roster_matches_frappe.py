# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The roster's role map says exactly what `frappe.get_roles` says, for every enabled user.

WHY THIS TEST EXISTS. `crm.api.session.get_users` used to call `frappe.get_roles(name)` once per user.
That function answers `is_system_user` through `get_cached_doc`, which loads the whole User document and
all eleven of its child tables to read one field — a fair trade asked once for the session user, which is
what it was built for, and 4,421 queries asked 282 times in a loop. `_roles_by_user` answers the same
question for the whole roster in one query, reading `user_type` off the row the endpoint already selected.

THAT MIRROR IS THE RISK. It restates rules that live in `frappe.permissions.get_roles`, so a framework
upgrade could change them underneath us with nothing to conflict on — the mirror is in the crm fork, the
rules are in frappe. This test is what turns that silent drift into a red run. It is deliberately a
comparison against the framework rather than a restatement of the rules a second time.

It also pins the coupling that has no other guard: the mirror reads `user_type` from the SELECT, so
dropping that field from the endpoint's field list would quietly stop granting `Desk User`.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.api.test_user_roster_matches_frappe
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from crm.api.session import _roles_by_user, _telephony_agents, get_users


class TestTheRosterMatchesFrappe(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.users = frappe.qb.get_query(
			"User", fields=["name", "user_type"], filters={"enabled": 1}
		).run(as_dict=1)

	def test_every_user_gets_the_roles_the_framework_gives_them(self):
		"""THE drift guard. Not a sample — every enabled user, against frappe's own answer."""
		mirrored = _roles_by_user(self.users)
		mismatched = [
			user.name
			for user in self.users
			if sorted(mirrored[user.name]) != sorted(frappe.get_roles(user.name))
		]
		self.assertEqual(
			mismatched, [], f"the roster's role map has drifted from frappe.get_roles for {len(mismatched)} users"
		)

	def test_the_endpoint_still_selects_the_field_the_mirror_reads(self):
		"""`user_type` is how `Desk User` is granted without loading a document. If the endpoint's field
		list loses it the mirror goes quiet rather than red, so the coupling is asserted here."""
		self.assertTrue(
			any(user.get("user_type") for user in get_users()[0]),
			"get_users stopped selecting user_type — the role mirror cannot see System Users",
		)

	def test_an_agent_lookup_is_the_docname_a_caller_already_expected(self):
		"""`db.exists` returned the agent's NAME, and the payload carries that value, not a bool. `user` is
		unique on the doctype, so one map entry per user is the whole answer."""
		agents = _telephony_agents()
		for user, name in list(agents.items())[:5]:
			self.assertEqual(frappe.db.exists("CRM Telephony Agent", {"user": user}), name)
		self.assertTrue(
			frappe.get_meta("CRM Telephony Agent").get_field("user").unique,
			"`user` is no longer unique — one agent per user is no longer a safe assumption",
		)
