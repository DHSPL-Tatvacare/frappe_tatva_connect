# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""VAPT authorization regression suite — one test per enforcement layer.

Written adversarially (TDD): every test asserts the SECURE end-state, so the suite is RED
before the fixes land and GREEN after. Maps 1:1 to the ApniSec audit + our layer model:

  L1  doctype matrix    -> a junk-ERP-role user cannot read/delete Contact          (VAPT #1)
  L2  row-scope (brain) -> a peer cannot see a Task on a lead they cannot see
  L3  method wrappers   -> a no-role user cannot reach the engine-bypassing native
                           crm methods add_task_to_call_log / get_assigned_users /
                           get_linked_docs_of_document                              (VAPT #2/#3/#6)
  L4  drift guard       -> no locked doctype grants All/Guest write/create/delete

Each layer is exercised through its REAL path: frappe.has_permission / get_list / delete_doc
for L1-L2, and the actual override dispatch (mirroring frappe.handler.execute_cmd) for L3 — a
direct import would bypass override_whitelisted_methods and give a FALSE green.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import seed

JUNK_ROLE = "Purchase Master Manager"  # unused ERPNext role still granted CRUD on Contact today
ATTACKER = "vapt-attacker@example.com"  # holds the junk role -> reproduces "reads/deletes all"
NOROLE = "vapt-norole@example.com"      # no roles -> for the engine-bypassing methods
OWNER = "vapt-owner@example.com"        # Sales User who owns the lead
PEER = "vapt-peer@example.com"          # Sales User who does NOT


def dispatch(cmd, **kwargs):
    """Mirror frappe.handler.execute_cmd's override resolution — the REAL HTTP dispatch path,
    so override_whitelisted_methods is honoured. A direct import would not be (false green)."""
    for hook in (frappe.get_hooks("override_whitelisted_methods") or {}).get(cmd, []):
        cmd = hook
        break
    return frappe.call(frappe.get_attr(cmd), **kwargs)


class TestVAPTAuthz(FrappeTestCase):
    def setUp(self):
        seed.sync_catalog()
        self._user(ATTACKER, [JUNK_ROLE])
        self._user(NOROLE, [])
        self._user(OWNER, ["Sales User"])
        self._user(PEER, ["Sales User"])
        self.contact = self._contact()
        self.lead = self._lead(OWNER)
        self._restrict(self.lead, OWNER)  # lead visible only to OWNER -> PEER genuinely can't see it
        self.call_log = self._call_log(self.lead)
        self.task = self._task(self.lead)

    def tearDown(self):
        frappe.set_user("Administrator")
        self._set_switch("Task::CRM Task::visibility", 0)

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
        d.type = "Incoming"; d.status = "Completed"
        setattr(d, "from", "111"); d.to = "222"
        d.reference_doctype, d.reference_docname = "CRM Lead", lead
        d.insert(ignore_permissions=True)
        return d.name

    def _task(self, lead):
        d = frappe.new_doc("CRM Task")
        d.title = "ZVAPT-TASK"; d.status = "Todo"
        d.reference_doctype, d.reference_docname = "CRM Lead", lead
        d.insert(ignore_permissions=True)
        return d.name

    def _set_switch(self, key, on):
        frappe.db.set_value("CRM Tatva Automation", key, "enabled", 1 if on else 0)

    def _as(self, user, fn):
        frappe.set_user(user)
        try:
            return fn()
        finally:
            frappe.set_user("Administrator")

    # ---------- L1: doctype matrix ----------
    def test_L1_junk_role_cannot_read_contact(self):
        seen = self._as(ATTACKER, lambda: frappe.has_permission("Contact", "read", self.contact))
        self.assertFalse(seen, "L1 BREACH: a junk-ERP-role user can READ contacts (VAPT #1)")

    def test_L1_junk_role_cannot_delete_contact(self):
        with self.assertRaises(frappe.PermissionError, msg="L1 BREACH: a junk-ERP-role user can DELETE a contact (VAPT #1)"):
            self._as(ATTACKER, lambda: frappe.delete_doc("Contact", self.contact))

    # ---------- L2: row-scope (brain) ----------
    def test_L2_peer_cannot_see_task_on_hidden_lead(self):
        self._set_switch("Task::CRM Task::visibility", 1)
        seen = self._as(PEER, lambda: frappe.has_permission("CRM Task", "read", self.task))
        self.assertFalse(seen, "L2 BREACH: a peer can see a Task on a lead they cannot see")

    # ---------- L3: override wrappers (must go through dispatch) ----------
    def test_L3_get_assigned_users_denied(self):
        with self.assertRaises(frappe.PermissionError, msg="L3 BREACH: no-role user reached get_assigned_users (VAPT #3)"):
            self._as(NOROLE, lambda: dispatch("crm.api.doc.get_assigned_users", doctype="CRM Lead", name=self.lead))

    def test_L3_add_task_to_call_log_denied(self):
        with self.assertRaises(frappe.PermissionError, msg="L3 BREACH: no-role user reached add_task_to_call_log (VAPT #2)"):
            self._as(NOROLE, lambda: dispatch("crm.integrations.api.add_task_to_call_log",
                     call_sid=self.call_log, task={"title": "x", "status": "Todo"}))

    def test_L3_get_linked_docs_denied(self):
        with self.assertRaises(frappe.PermissionError, msg="L3 BREACH: no-role user reached get_linked_docs_of_document (VAPT #6)"):
            self._as(NOROLE, lambda: dispatch("crm.api.doc.get_linked_docs_of_document", doctype="CRM Lead", docname=self.lead))

    # ---------- L4: drift guard ----------
    def test_L4_locked_doctypes_have_no_all_or_guest_crud(self):
        locked = ["Contact"]
        bad = frappe.db.sql(
            """
            select parent, role from `tabDocPerm`
              where parent in %(d)s and role in ('All','Guest') and (`write`=1 or `create`=1 or `delete`=1)
            union
            select parent, role from `tabCustom DocPerm`
              where parent in %(d)s and role in ('All','Guest') and (`write`=1 or `create`=1 or `delete`=1)
            """,
            {"d": locked},
        )
        self.assertEqual(bad, [], f"L4 BREACH: locked doctype(s) still open to All/Guest: {bad}")
