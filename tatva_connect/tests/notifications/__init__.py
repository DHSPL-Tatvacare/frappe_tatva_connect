import frappe


def tick_bell(*users):
	"""Each rep has System notifications on and ticks every notice we write for their bell, through the document API, as the Settings screen saves it."""
	from frappe.desk.doctype.notification_settings.notification_settings import create_notification_settings

	from tatva_connect.notifications import api, dispatch

	for user in users:
		if not frappe.db.exists(api.USER_SETTINGS, user):
			create_notification_settings(user)
		doc = frappe.get_doc(api.USER_SETTINGS, user)
		doc.set("enabled", 1)  # frappe's own master; an earlier suite may have left it off
		doc.set(api.BELL_TYPES_FIELD, [{"notification_type": t} for t in dispatch.TYPES])
		doc.save(ignore_permissions=True)
