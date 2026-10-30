# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""One Tatva Connect: the Tatva Setup app icon goes and its spaces sit in the Tatva Connect folder, as the dock already lists them (file archived in .archive/tatva-setup-folder-retired-2026-10-30)."""
import frappe

ICON = "Desktop Icon"


def execute():
	# An Apps-mode site never imported icon rows; one that did keeps the folder's spaces, under the one app.
	frappe.db.delete(ICON, {"name": "Tatva Setup", "icon_type": "App", "app": "tatva_connect"})
	for name in frappe.get_all(ICON, filters={"app": "tatva_connect", "parent_icon": "Tatva Setup"}, pluck="name"):
		frappe.db.set_value(ICON, name, "parent_icon", "Tatva Connect", update_modified=False)
	frappe.cache.delete_key("desktop_icons")
