"""Give every acquisition row an address.

`acq` became multi-row, keyed by `touch_at`. A row whose key is blank has no address at all: the section
validator says so, the partner API can never target it, and every later write appends beside it instead of
updating it. Existing rows predate the column, so they are stamped with the moment their lead was created —
the closest true statement available about when that acquisition happened.

End state: no `CRM Acquisition Profile` row hanging off a lead carries a blank `touch_at`. Idempotent, and
assumes nothing about what ran before: it stamps whatever is still blank, however it got that way.
"""
import frappe


def execute():
	if not frappe.db.has_column("CRM Acquisition Profile", "touch_at"):
		return  # the doctype JSON has not landed yet; nothing to address

	frappe.db.sql(
		"""
		UPDATE `tabCRM Acquisition Profile` acq
		JOIN `tabCRM Lead` lead ON lead.name = acq.parent
		SET acq.touch_at = lead.creation
		WHERE acq.parenttype = 'CRM Lead' AND IFNULL(acq.touch_at, '') = ''
		"""
	)
	# A row whose parent is gone or is not a lead still must not carry a blank key.
	frappe.db.sql(
		"""
		UPDATE `tabCRM Acquisition Profile`
		SET touch_at = creation
		WHERE IFNULL(touch_at, '') = ''
		"""
	)
