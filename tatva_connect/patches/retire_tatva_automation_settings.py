"""Retire the orphaned `Tatva Automation Settings` single: copy its two WhatsApp template caps onto CRM WhatsApp Settings (without clobbering set values), then delete the DocType + Singles rows; idempotent."""
import frappe

_OLD = "Tatva Automation Settings"
_NEW = "CRM WhatsApp Settings"
_FIELDS = ("template_cap_per_hour", "template_cap_per_day")


def execute():
	if frappe.db.exists("DocType", _OLD):
		for field in _FIELDS:
			old = frappe.db.get_value("Singles", {"doctype": _OLD, "field": field}, "value", order_by=None)
			if old in (None, ""):
				continue
			new = frappe.db.get_value("Singles", {"doctype": _NEW, "field": field}, "value", order_by=None)
			if new in (None, ""):  # don't overwrite an already-set WATI cap
				frappe.db.set_single_value(_NEW, field, old)
		frappe.delete_doc("DocType", _OLD, ignore_permissions=True, force=True)
	# Drop any lingering Singles rows (covers a prior partial run where the DocType was already gone).
	frappe.db.delete("Singles", {"doctype": _OLD})
	frappe.db.commit()
