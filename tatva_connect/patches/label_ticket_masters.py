"""Each ticket sub type, bucket and resolution reason takes its name as its new label, so the picker shows what it showed before and new rows key on the label."""
import frappe

MASTERS = ("HD Ticket Sub Type", "HD Ticket Bucket", "HD Ticket Resolution Reason")


def execute():
	"""Idempotent: only a row with no label is filled, and its name is left as it is."""
	for doctype in MASTERS:
		for name in frappe.get_all(doctype, filters={"label": ["in", [""]]}, pluck="name"):
			frappe.db.set_value(doctype, name, "label", name, update_modified=False)
