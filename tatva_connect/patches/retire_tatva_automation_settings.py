"""One-time: retire the orphaned `Tatva Automation Settings` single.

It lost its only switch in the automation-seam refactor and held just the two WhatsApp
template caps — a misnamed cousin of `CRM Tatva Automation`. The caps are WhatsApp config, so
they move onto `CRM WhatsApp Settings`. Copy each value forward first (preserve a prod operator's
tuned cap, and never clobber a value already set on WhatsApp Settings), then delete the DocType
and its Singles rows so a re-run no-ops. On a fresh install the old single never existed -> clean no-op.
"""
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
