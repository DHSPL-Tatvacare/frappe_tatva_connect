# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Builder discovery + Web Form publish toggle for the CRM Intake Form Desk form.

Read-only schema discovery (`list_target_fields`) and a single guarded mutation
(`toggle_published`) that flips the linked Web Form's `published`. Both gate on
`frappe.has_permission("CRM Intake Form", ..., throw=True)` — the same discipline as
every other tatva_connect whitelisted method. No DDL, no eval, no user-string execution:
the field list comes straight from live `get_meta`, and the only thing written is the
boolean `published` on the form the scaffolder already owns.
"""
import frappe
from frappe import _

# ONE resolver, shared with the save-time validation — target_table -> the doctype whose
# fields can be picked (lead = CRM Lead; child tables -> the child doctype; note -> None).
from tatva_connect.intake.intake import target_doctype as _resolve_doctype
from tatva_connect.lead import mapping


@frappe.whitelist()
def list_target_fields(target_table, intake_form=None, vertical=None, group=None, program=None):
	"""The fields this form may map into `target_table`, as [{fieldname, label, fieldtype}].

	The list is the ONE mapping seam (`lead/mapping.py`), scoped to the section AND to the form's grain —
	NOT a raw get_meta walk, which offered every column on the doctype including ones this grain must
	never write. The grain axes come from the CALLER (the open, possibly unsaved builder form), so the
	list narrows the moment an operator picks a grain — no save, no round trip.

	Gated read-only: requires read on CRM Intake Form (System-Manager-only doctype).
	"""
	frappe.has_permission("CRM Intake Form", "read", throw=True)

	if not _resolve_doctype(target_table):
		return []  # note (free-text) or unknown table -> nothing to pick

	grain = ((vertical or "").strip(), (group or "").strip(), (program or "").strip())
	if not any(grain):
		return []  # no grain chosen yet — the client shows "pick the grain first"

	return [{"fieldname": f["fieldname"], "label": f["label"], "fieldtype": f["fieldtype"]}
	        for f in mapping.mappable_fields(section=target_table, grain=grain)]


@frappe.whitelist()
def toggle_published(intake_form):
	"""Flip the linked Web Form's `published` for one CRM Intake Form. Gated on write to
	CRM Intake Form; resolves the form via the scaffolder's derived doctype name, so the
	client never names a Web Form directly. Returns the new published state (0/1)."""
	frappe.has_permission("CRM Intake Form", "write", throw=True)

	from tatva_connect.intake.builder import doctype_name_for

	cfg = frappe.get_doc("CRM Intake Form", intake_form)
	dt = doctype_name_for(cfg)
	wf_name = frappe.db.get_value("Web Form", {"doc_type": dt}, "name")
	if not wf_name:
		frappe.throw(_("No Web Form is linked yet — Save the form first."), title=_("Not Published"))

	new_state = 0 if frappe.db.get_value("Web Form", wf_name, "published") else 1
	frappe.db.set_value("Web Form", wf_name, "published", new_state)
	return new_state
