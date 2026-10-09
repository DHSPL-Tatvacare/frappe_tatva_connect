"""A Smart View is never shared with the whole site, so every existing everyone share on one is removed once."""
import frappe


def execute():
	for name in frappe.get_all("DocShare", filters={"share_doctype": "CRM Smart View", "everyone": 1}, pluck="name"):
		frappe.delete_doc("DocShare", name, ignore_permissions=True)
