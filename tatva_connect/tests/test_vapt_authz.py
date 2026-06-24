# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""VAPT authorization regression suite — adversarial, organised by enforcement layer.

Written TDD: every test asserts the SECURE end-state, so the suite is RED before the fixes and
GREEN after. Each layer is exercised through its REAL path (frappe.has_permission / get_list /
delete_doc, and the actual override dispatch for native methods — a direct import would bypass
override_whitelisted_methods and give a false green).

  L1  doctype matrix    — junk/no-role users can't read/delete Contact; legit roles preserved
  L2  row-scope (brain) — a peer can't see a Task on a lead they can't see
  L3  method gates      — for EVERY engine-bypassing native method (data-driven):
                            • no-role is denied                         (privilege escalation)
                            • a peer is denied on ANOTHER's record      (adversarial escalation)
                            • an authorized owner is NOT denied         (no regression)
  L4  drift guard       — locked doctypes stay closed to All/Guest
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.access import lockdown
from tatva_connect.automation import seed

JUNK_ROLE = "Purchase Master Manager"  # unused ERPNext role; reproduces "reads all" on Contact
ATTACKER = "vapt-attacker@example.com"  # junk role
NOROLE = "vapt-norole@example.com"      # no roles
OWNER = "vapt-owner@example.com"        # Sales User — owns the data
PEER = "vapt-peer@example.com"          # Sales User — NOT on the data
MANAGER = "vapt-manager@example.com"    # Sales Manager

# brain switches that scope the doc-specific gates (Lead/Call Log visibility) for the peer test
BRAIN = [
	"Task::CRM Task::visibility",
	"Telephony::CRM Call Log::visibility",
	"Note::FCRM Note::visibility",
	"WhatsApp::WhatsApp Message::visibility",
]


def dispatch(cmd, **kwargs):
	"""Mirror frappe.handler.execute_cmd's override resolution — the REAL HTTP dispatch path,
	so override_whitelisted_methods is honoured (a direct import would not be: false green)."""
	for hook in (frappe.get_hooks("override_whitelisted_methods") or {}).get(cmd, []):
		cmd = hook
		break
	return frappe.call(frappe.get_attr(cmd), **kwargs)


class TestVAPTAuthz(FrappeTestCase):
	def setUp(self):
		seed.sync_catalog()
		for u, roles in [(ATTACKER, [JUNK_ROLE]), (NOROLE, []), (OWNER, ["Sales User"]),
						 (PEER, ["Sales User"]), (MANAGER, ["Sales Manager"])]:
			self._user(u, roles)
		self.contact = self._contact()
		self.lead = self._lead(OWNER)
		self._restrict(self.lead, OWNER)  # only OWNER can see the lead -> PEER genuinely cannot
		self.call_log = self._call_log(self.lead)
		self.task = self._task(self.lead)
		self.deal = self._deal(OWNER)
		self.fx = {"lead": self.lead, "call_log": self.call_log, "deal": self.deal, "contact": self.contact}

	def tearDown(self):
		frappe.set_user("Administrator")
		for key in BRAIN:
			self._switch(key, 0)

	# data-driven spec: (cmd, kwargs(fixtures), escalation_ref)
	#   escalation_ref = the record a PEER cannot see (Lead/Call Log); None when the gate is
	#   doctype-level (a Sales User legitimately passes it, so only no-role is denied).
	L3 = [
		("crm.api.doc.get_assigned_users", lambda f: dict(doctype="CRM Lead", name=f["lead"]), "lead"),
		("crm.api.doc.get_linked_docs_of_document", lambda f: dict(doctype="CRM Lead", docname=f["lead"]), "lead"),
		("crm.api.whatsapp.get_whatsapp_messages", lambda f: dict(reference_doctype="CRM Lead", reference_name=f["lead"]), "lead"),
		("crm.integrations.api.add_task_to_call_log", lambda f: dict(call_sid=f["call_log"], task={"title": "x", "status": "Todo"}), "call_log"),
		("crm.integrations.api.add_note_to_call_log", lambda f: dict(call_sid=f["call_log"], note={"content": "x"}), "call_log"),
		("crm.integrations.api.get_recording_url", lambda f: dict(call_log_name=f["call_log"]), "call_log"),
		("crm.fcrm.doctype.crm_deal.api.get_deal_contacts", lambda f: dict(name=f["deal"]), None),
		("crm.fcrm.doctype.crm_deal.crm_deal.add_contact", lambda f: dict(deal=f["deal"], contact=f["contact"]), None),
		("crm.fcrm.doctype.crm_deal.crm_deal.remove_contact", lambda f: dict(deal=f["deal"], contact=f["contact"]), None),
		("crm.fcrm.doctype.crm_deal.crm_deal.set_primary_contact", lambda f: dict(deal=f["deal"], contact=f["contact"]), None),
		("crm.fcrm.doctype.crm_deal.crm_deal.create_deal", lambda f: dict(doc={"lead": f["lead"]}), None),
		("crm.integrations.api.get_contact_by_phone_number", lambda f: dict(phone_number="999"), None),
		("crm.integrations.api.get_contact_lead_or_deal_from_number", lambda f: dict(number="999"), None),
		("crm.integrations.api.set_default_calling_medium", lambda f: dict(medium="Acefone"), None),
	]

	# ---------- L1: doctype matrix (Contact) ----------
	def test_L1_attacker_cannot_read_contacts(self):
		for u in (NOROLE, ATTACKER):
			self.assertFalse(self._as(u, lambda: frappe.has_permission("Contact", "read", self.contact)),
							 f"L1 BREACH: {u} can READ contacts (VAPT #1)")

	def test_L1_attacker_cannot_delete_contact(self):
		with self.assertRaises(frappe.PermissionError, msg="L1 BREACH: junk-role user can DELETE a contact (VAPT #1)"):
			self._as(ATTACKER, lambda: frappe.delete_doc("Contact", self.contact))

	def test_L1_legit_access_preserved(self):
		self.assertTrue(self._as(OWNER, lambda: frappe.has_permission("Contact", "read", self.contact)),
						"L1 REGRESSION: Sales User lost Contact read")
		self.assertFalse(self._as(OWNER, lambda: frappe.has_permission("Contact", "delete", self.contact)),
						 "L1: a rep should not be able to delete a contact")
		self.assertTrue(self._as(MANAGER, lambda: frappe.has_permission("Contact", "delete", self.contact)),
						"L1 REGRESSION: Sales Manager lost Contact delete")

	# ---------- L2: row-scope (brain) ----------
	def test_L2_peer_cannot_see_task_on_hidden_lead(self):
		self._switch("Task::CRM Task::visibility", 1)
		self.assertFalse(self._as(PEER, lambda: frappe.has_permission("CRM Task", "read", self.task)),
						 "L2 BREACH: a peer can see a Task on a lead they cannot see")

	# ---------- L3: no-role denied (privilege escalation) ----------
	def test_L3_no_role_denied(self):
		for cmd, kw, _ in self.L3:
			with self.subTest(cmd=cmd):
				with self.assertRaises(frappe.PermissionError, msg=f"L3 BREACH: no-role reached {cmd}"):
					self._as(NOROLE, lambda cmd=cmd, kw=kw: dispatch(cmd, **kw(self.fx)))

	# ---------- L3: peer denied on another's record (adversarial escalation) ----------
	def test_L3_peer_cannot_act_on_others_record(self):
		for key in BRAIN:
			self._switch(key, 1)
		for cmd, kw, esc in self.L3:
			if not esc:
				continue
			with self.subTest(cmd=cmd):
				with self.assertRaises(frappe.PermissionError, msg=f"L3 ESCALATION: peer reached {cmd} on another's {esc}"):
					self._as(PEER, lambda cmd=cmd, kw=kw: dispatch(cmd, **kw(self.fx)))

	# ---------- L3: authorized NOT denied (no regression) ----------
	def test_L3_authorized_not_denied(self):
		for key in BRAIN:
			self._switch(key, 1)
		for cmd, kw, _ in self.L3:
			with self.subTest(cmd=cmd):
				try:
					self._as(OWNER, lambda cmd=cmd, kw=kw: dispatch(cmd, **kw(self.fx)))
				except frappe.PermissionError as e:
					self.fail(f"L3 REGRESSION: authorized owner denied on {cmd}: {e}")
				except Exception:
					pass  # native errors (DoesNotExist / validation) are fine — not our gate

	# ---------- L4: drift guard ----------
	def test_L4_locked_doctypes_have_no_all_or_guest_crud(self):
		bad = [g for dt in lockdown.LOCKED_MATRIX for g in lockdown.effective_all_guest_grants(dt)]
		self.assertEqual(bad, [], f"L4 BREACH: locked doctype(s) still open to All/Guest: {bad}")

	# ---------- fixtures ----------
	def _user(self, email, roles):
		if frappe.db.exists("User", email):
			frappe.delete_doc("User", email, force=True, ignore_permissions=True)
		frappe.get_doc({
			"doctype": "User", "email": email, "first_name": email.split("@")[0],
			"send_welcome_email": 0, "roles": [{"role": r} for r in roles],
		}).insert(ignore_permissions=True)

	def _contact(self):
		c = frappe.get_doc({"doctype": "Contact", "first_name": "ZVAPT-CONTACT"})
		c.insert(ignore_permissions=True)
		return c.name

	def _lead(self, owner):
		d = frappe.get_doc({"doctype": "CRM Lead", "first_name": "ZVAPT-LEAD", "status": "New", "lead_owner": owner})
		d.insert(ignore_permissions=True)
		d.db_set("owner", owner)
		return d.name

	def _restrict(self, lead, user):
		frappe.get_doc({"doctype": "User Permission", "user": user, "allow": "CRM Lead", "for_value": lead}).insert(ignore_permissions=True)

	def _call_log(self, lead):
		d = frappe.new_doc("CRM Call Log")
		d.id = frappe.generate_hash(length=12)
		d.type, d.status = "Incoming", "Completed"
		setattr(d, "from", "111")
		d.to = "222"
		d.reference_doctype, d.reference_docname = "CRM Lead", lead
		d.insert(ignore_permissions=True)
		return d.name

	def _task(self, lead):
		d = frappe.new_doc("CRM Task")
		d.title, d.status = "ZVAPT-TASK", "Todo"
		d.reference_doctype, d.reference_docname = "CRM Lead", lead
		d.insert(ignore_permissions=True)
		return d.name

	def _deal(self, owner):
		# an OPEN status — terminal ones (Lost/Won) demand extra fields (lost reason, etc.)
		statuses = frappe.get_all("CRM Deal Status", filters={"type": "Open"}, limit=1, pluck="name") \
			or frappe.get_all("CRM Deal Status", order_by="position", limit=1, pluck="name")
		d = frappe.get_doc({"doctype": "CRM Deal", "status": statuses[0] if statuses else None})
		d.insert(ignore_permissions=True)
		d.db_set("owner", owner)
		return d.name

	def _switch(self, key, on):
		frappe.db.set_value("CRM Tatva Automation", key, "enabled", 1 if on else 0)

	def _as(self, user, fn):
		frappe.set_user(user)
		try:
			return fn()
		finally:
			frappe.set_user("Administrator")
