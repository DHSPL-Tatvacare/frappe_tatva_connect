# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""WhatsApp Message inherits Lead/Deal scope via the shared row-visibility brain (Leg 2, Batch 1).

With the WhatsApp::WhatsApp Message::visibility switch ON: a user who cannot see a lead cannot
see or open a WhatsApp message threaded on it; a user who can see the lead can. With the switch
OFF: stock crm (no scoping) — any role-permitted user sees every message. WhatsApp Message links
its parent via reference_doctype + `reference_name` (the field name differs from CRM Task's
reference_docname; the brain's resolver + PQC pick the right column).
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.access import visibility
from tatva_connect.automation import seed

_SWITCH = "WhatsApp::WhatsApp Message::visibility"


class TestWhatsAppMessageScope(FrappeTestCase):
	def setUp(self):
		seed.sync_catalog()
		self._set_switch(1)
		self._made = []
		# in_scope owns the lead; out_scope shares the Sales User role but not the lead.
		self.in_scope = self._user("wamsg-in@example.com", "Sales User")
		self.out_scope = self._user("wamsg-out@example.com", "Sales User")
		self.lead = self._lead(self.in_scope)
		# Restrict the lead to in_scope only, so out_scope genuinely can't read it.
		self._restrict_lead(self.lead, self.in_scope)
		self.msg = self._message(("CRM Lead", self.lead))

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
		lead's own read scope, and therefore the message's inherited scope."""
		frappe.get_doc({
			"doctype": "User Permission", "user": user,
			"allow": "CRM Lead", "for_value": lead,
		}).insert(ignore_permissions=True)

	def _message(self, reference):
		# Incoming + the ingested flag: the WATI override's send seams only fire on Outgoing,
		# and tatva_ingested short-circuits any provider call — so this inserts a pure record.
		doc = frappe.new_doc("WhatsApp Message")
		doc.type = "Incoming"
		doc.content_type = "text"
		doc.message = "secret"
		doc.message_id = frappe.generate_hash(length=12)
		doc.reference_doctype, doc.reference_name = reference
		doc.flags.tatva_ingested = True
		# Skip validate: the WATI override's validate demands a WATI Account Routing rule for the
		# lead's grain, which this scope test doesn't configure. We only need the persisted record
		# + its parent linkage; ignore_validate inserts a clean row (reference_name is pinned below
		# since crm's validate rewrite is skipped too).
		doc.flags.ignore_validate = True
		doc.insert(ignore_permissions=True)
		# crm's validate can rewrite reference_name to first-by-phone; pin it back explicitly
		# so the test's parent linkage is deterministic.
		doc.db_set("reference_name", reference[1])
		self._made.append(("WhatsApp Message", doc.name))
		return frappe.get_doc("WhatsApp Message", doc.name)

	# --- switch ON: scope enforced --------------------------------------
	def test_in_scope_user_can_see(self):
		self.assertTrue(
			visibility.scoped_has_permission(self.msg, "read", self.in_scope),
			f"in-scope user denied on {self.msg.name}",
		)

	def test_out_of_scope_user_blocked(self):
		self.assertFalse(
			visibility.scoped_has_permission(self.msg, "read", self.out_scope),
			f"out-of-scope user saw {self.msg.name}",
		)

	def test_out_of_scope_blocked_via_frappe_has_permission(self):
		"""The hook is wired, so frappe.has_permission (what the SPA's WhatsApp tab calls) denies too."""
		frappe.set_user(self.out_scope)
		try:
			self.assertFalse(
				frappe.has_permission("WhatsApp Message", "read", self.msg.name),
			)
		finally:
			frappe.set_user("Administrator")

	def test_pqc_scopes_list(self):
		pqc = visibility.scoped_pqc("WhatsApp Message", self.out_scope)
		self.assertIn("`tabWhatsApp Message`.`owner`=", pqc)
		# No assigned_to column on WhatsApp Message -> that clause must NOT appear.
		self.assertNotIn("assigned_to", pqc)
		self.assertIn("reference_doctype", pqc)
		# WhatsApp Message uses `reference_name`, NOT `reference_docname`.
		self.assertIn("reference_name", pqc)
		self.assertNotIn("reference_docname", pqc)

	# --- switch OFF: stock crm ------------------------------------------
	def test_switch_off_is_stock(self):
		self._set_switch(0)
		self.assertEqual(visibility.scoped_pqc("WhatsApp Message", self.out_scope), "")
		self.assertTrue(visibility.scoped_has_permission(self.msg, "read", self.out_scope))
