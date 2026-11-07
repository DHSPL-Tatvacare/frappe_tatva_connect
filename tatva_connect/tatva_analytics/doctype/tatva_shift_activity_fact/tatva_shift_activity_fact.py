import frappe
from frappe.model.document import Document


class TatvaShiftActivityFact(Document):
	pass


def on_doctype_update():
	# A board reads one business line over a date range and a person's days; the job rewrites a person's days.
	frappe.db.add_index("Tatva Shift Activity Fact", ["grain_vertical", "grain_group", "grain_program", "day"])
	frappe.db.add_index("Tatva Shift Activity Fact", ["user", "day"])
	frappe.db.add_index("Tatva Shift Activity Fact", ["day"])
