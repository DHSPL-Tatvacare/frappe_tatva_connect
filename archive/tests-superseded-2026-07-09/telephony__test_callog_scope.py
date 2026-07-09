# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""CRM Call Log inherits Lead/Deal scope via the shared row-visibility brain (Leg 2, Batch 0).

With the Telephony::CRM Call Log::visibility switch ON: a user who cannot see a lead cannot
see or open a call log on it; a user who can see the lead can. With the switch OFF: stock crm
(no scoping) — any role-permitted user sees every call log. Covers both the `links`-table and
`reference_*` linkages crm uses, and the single-doc has_permission gate behind bridge.get_call_log.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.access import visibility
from tatva_connect.automation import seed

_SWITCH = "Telephony::CRM Call Log::visibility"


class TestCallLogScope(FrappeTestCase):
	def setUp(self):
		seed.sync_catalog()
		self._set_switch(1)
		self._made = []
		# in_scope owns the lead; out_scope shares the Sales User role but not the lead.
		self.in_scope = self._user("call-in@example.com", "Sales User")
		self.out_scope = self._user("call-out@example.com", "Sales User")
		self.lead = self._lead(self.in_scope)
		# Restrict the lead to in_scope only, so out_scope genuinely can't read it.
		self._restrict_lead(self.lead, self.in_scope)
		self.call_ref = self._call(reference=("CRM Lead", self.lead))
		self.call_links = self._call(reference=None, link=("CRM Lead", self.lead))

	def tearDown(self):
		frappe.set_user("Administrator")
		for dt, name in reversed(self._made):
			frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
		frappe.db.delete("User Permission", {"allow": "CRM Lead", "for_value": self.lead})
		self._set_switch(0)

	# --- helpers ---------------------------------------------------------
	def _set_switch(self, on):
		frappe.db.set_value("CRM Tatva Automation", _SWITCH, "enabled", 1 if on else 0)

	def _user(self, email, role):
		if not frappe.db.exists("User", email):
			frappe.get_doc({
				"doctype": "User", "email": email, "first_name": email.split("@")[0],
				"send_welcome_email": 0, "roles": [{"role": role}],
			}).insert(ignore_permissions=True)
		return email

	def _lead(self, owner):
		ld = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Scope", "lead_name": "Scope Lead",
			"status": "New", "lead_owner": owner,
		})
		ld.insert(ignore_permissions=True)
		ld.db_set("owner", owner)
		self._made.append(("CRM Lead", ld.name))
		return ld.name

	def _restrict_lead(self, lead, user):
		"""A User Permission limiting this lead to `user` — so any OTHER Sales User fails the
		lead's own read scope, and therefore the call log's inherited scope."""
		frappe.get_doc({
			"doctype": "User Permission", "user": user,
			"allow": "CRM Lead", "for_value": lead,
		}).insert(ignore_permissions=True)

	def _call(self, reference, link=None):
		doc = frappe.new_doc("CRM Call Log")
		doc.id = frappe.generate_hash(length=12)
		doc.type = "Outgoing"
		doc.status = "Completed"
		setattr(doc, "from", "1000")
		doc.to = "2000"
		if reference:
			doc.reference_doctype, doc.reference_docname = reference
		if link:
			doc.append("links", {"link_doctype": link[0], "link_name": link[1]})
		doc.insert(ignore_permissions=True)
		self._made.append(("CRM Call Log", doc.name))
		return frappe.get_doc("CRM Call Log", doc.name)

	# --- switch ON: scope enforced --------------------------------------
	def test_in_scope_user_can_see(self):
		self.assertTrue(
			visibility.scoped_has_permission(self.call_ref, "read", self.in_scope),
			f"in-scope user denied on {self.call_ref.name}",
		)

	def test_links_only_call_fails_closed(self):
		"""ONE rule: we scope on reference_* — the canonical linkage every Acefone/partner call
		log sets. A links-only call log (no reference parent) is an orphan -> DENIED for any
		non-owner, including the in-scope user. Fail-closed; the single-doc gate and the
		reference-only list PQC agree exactly (no asymmetry)."""
		self.assertFalse(
			visibility.scoped_has_permission(self.call_links, "read", self.in_scope),
		)

	def test_out_of_scope_user_blocked(self):
		for call in (self.call_ref, self.call_links):
			self.assertFalse(
				visibility.scoped_has_permission(call, "read", self.out_scope),
				f"out-of-scope user saw {call.name}",
			)

	def test_out_of_scope_blocked_via_frappe_has_permission(self):
		"""The hook is wired, so frappe.has_permission (what bridge.get_call_log calls) denies too."""
		frappe.set_user(self.out_scope)
		try:
			self.assertFalse(
				frappe.has_permission("CRM Call Log", "read", self.call_ref.name),
			)
		finally:
			frappe.set_user("Administrator")

	def test_pqc_scopes_list(self):
		pqc = visibility.scoped_pqc("CRM Call Log", self.out_scope)
		self.assertIn("`tabCRM Call Log`.`owner`=", pqc)
		# No assigned_to column on Call Log -> that clause must NOT appear.
		self.assertNotIn("assigned_to", pqc)
		self.assertIn("reference_doctype", pqc)

	# --- switch OFF: stock crm ------------------------------------------
	def test_switch_off_is_stock(self):
		self._set_switch(0)
		self.assertEqual(visibility.scoped_pqc("CRM Call Log", self.out_scope), "")
		self.assertTrue(visibility.scoped_has_permission(self.call_ref, "read", self.out_scope))
