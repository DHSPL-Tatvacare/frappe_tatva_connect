import frappe
from frappe.model.document import Document


class TatvaLeadAllocationFact(Document):
	pass


def on_doctype_update():
	# A board reads one business line over a date range and a person's rows by day; the job ages rows out by day.
	frappe.db.add_index("Tatva Lead Allocation Fact", ["grain_vertical", "grain_group", "grain_program", "day"])
	frappe.db.add_index("Tatva Lead Allocation Fact", ["user", "day"])
	frappe.db.add_index("Tatva Lead Allocation Fact", ["day"])
