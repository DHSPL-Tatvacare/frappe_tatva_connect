# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A Partner API user on a grain, granted as production grants one: the Partner API User role and an enabled mapping."""
import frappe

from tatva_connect.api import partner_activity
from tatva_connect.api._base import _load_caller


def make_partner(email, grain, allowed_fields=()):
	frappe.get_doc({"doctype": "User", "email": email, "first_name": "ZZ Partner", "user_type": "System User",
	                "send_welcome_email": 0}).insert(ignore_permissions=True)
	frappe.get_doc("User", email).add_roles("Partner API User")
	frappe.get_doc({"doctype": "CRM Lead API Mapping", "partner_user": email, "enabled": 1, "contract_name": email,
	                "vertical": grain["vertical"], "crm_group": grain["group"], "program": grain["program"],
	                "allowed_fields": [{"field": f} for f in allowed_fields]}).insert(ignore_permissions=True)
	return email


def create_activity(partner, lead, type_name, values):
	"""What `activity_create` does for this partner, after the HTTP layer; returns the task name."""
	frappe.set_user(partner)
	try:
		_user, mp, is_sysmgr = _load_caller()
		return partner_activity._create_one({"lead": lead, "task_type": type_name, "values": values}, mp, is_sysmgr)["name"]
	finally:
		frappe.set_user("Administrator")
