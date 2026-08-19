"""Give every existing CRM Plan Profile row an address, now that `plan` is multi-row.

`plan` became `is_multi_row=1` keyed on `custom_plan_assigned_date`, because a patient buys more than once
and single-row meant the second Post Sales punch overwrote the first — BCA Device, CGM, Nutraceuticals,
Duration, Drug Name and Diagnostics all lost their earlier answer, while `products` beside it kept both.

A BLANK ROW KEY IS NO ADDRESS AT ALL: the partner API can never target such a row, and every later write
appends beside it instead of correcting it. Existing rows predate the key — 4 of 4,802 carry one — so they
are stamped from their lead's creation, the closest true statement about when that plan was assigned. Same
defect, same cure and same shape as `stamp_acquisition_touch_at`, which did this for `acq`.

Only rows with a BLANK key are touched; a row that already carries one keeps it. Idempotent. No DDL.
"""

import frappe


def execute():
	if not frappe.db.has_column("CRM Plan Profile", "custom_plan_assigned_date"):
		return
	n = frappe.db.sql("""SELECT COUNT(*) FROM `tabCRM Plan Profile` p
	                     JOIN `tabCRM Lead` l ON l.name = p.parent
	                    WHERE p.parenttype = 'CRM Lead' AND p.custom_plan_assigned_date IS NULL""")[0][0]
	if not n:
		return
	frappe.db.sql("""UPDATE `tabCRM Plan Profile` p
	                   JOIN `tabCRM Lead` l ON l.name = p.parent
	                    SET p.custom_plan_assigned_date = l.creation
	                  WHERE p.parenttype = 'CRM Lead' AND p.custom_plan_assigned_date IS NULL""")
	frappe.db.commit()
	print(f"  stamp_plan_assigned_date: {n} plan row(s) addressed from their lead's creation")
