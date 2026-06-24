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

# One source for the target_table -> child-doctype map: import it from intake.py,
# never duplicate. lead -> CRM Lead; plan/care/lab/drug -> the child profile; note -> [].
from tatva_connect.intake.intake import _TABLE


def _resolve_doctype(target_table: str) -> str | None:
	"""target_table -> the doctype whose fields can be picked. lead = CRM Lead; the child
	profiles via the shared _TABLE; note has no schema (free-text Note title) -> None."""
	table = (target_table or "").strip()
	if table == "lead":
		return "CRM Lead"
	return _TABLE.get(table)  # None for "note" / anything unknown


@frappe.whitelist()
def list_target_fields(target_table, intake_form=None):
	"""Return the pickable fields of the doctype `target_table` resolves to, as
	[{fieldname, label, fieldtype}] from LIVE get_meta — never a baked list.

	Gated read-only: requires read on CRM Intake Form (System-Manager-only doctype), so
	this surfaces schema metadata to builders only. `intake_form` is accepted for symmetry
	with the client call but unused — the field set is a function of the target doctype.
	"""
	frappe.has_permission("CRM Intake Form", "read", throw=True)

	dt = _resolve_doctype(target_table)
	if not dt:
		return []  # note (free-text) or unknown table -> nothing to pick

	meta = frappe.get_meta(dt)
	out = []
	for df in meta.fields:
		# Skip layout-only fieldtypes — they are not data targets.
		if df.fieldtype in frappe.model.no_value_fields:
			continue
		out.append({"fieldname": df.fieldname, "label": _(df.label or df.fieldname), "fieldtype": df.fieldtype})
	return out


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
