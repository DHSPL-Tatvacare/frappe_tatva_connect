"""The CRM bell now reads frappe's Notification Log, whose old CRM rows no rep ever saw as unread; mark them read once so no badge jumps on deploy."""
import frappe

from tatva_connect.notifications.tray import APPS, LOG


def execute():
	if not frappe.db.has_column(LOG, "app"):
		return
	frappe.db.set_value(LOG, {"read": 0, "app": ["in", APPS]}, "read", 1, update_modified=False)
