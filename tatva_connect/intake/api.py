# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""The Desk form's read + command surface for CRM Intake Form. Permission gates that DELEGATE.

Nothing here writes a Web Form. `list_target_fields` delegates to the mapping seam,
`toggle_published` to `builder.publish`, `form_state` to `builder.readiness` — so the field list,
the publish write and the readiness decision each live in exactly one place, and this module is
only the gate in front of them. Every method gates on
`frappe.has_permission("CRM Intake Form", ..., throw=True)`, the same discipline as every other
tatva_connect whitelisted method. No DDL, no eval, no user-string execution.
"""
import frappe

from tatva_connect.intake import builder

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
	"""Take one form live, or withdraw it. Gated on write to CRM Intake Form, then DELEGATED —
	`builder` is the one writer of the Web Form, exactly as `list_target_fields` delegates to
	`mapping`. Returns the new published state."""
	frappe.has_permission("CRM Intake Form", "write", throw=True)

	cfg = frappe.get_doc("CRM Intake Form", intake_form)
	return builder.publish(cfg, not _published(cfg))


@frappe.whitelist()
def form_state(intake_form):
	"""Everything the Desk form paints, in ONE call: is it live, at what address, why it cannot go
	live, and the grain it stamps. The script decides none of this — `readiness` is the server's
	one answer, and the script only renders it (N3)."""
	frappe.has_permission("CRM Intake Form", "read", throw=True)

	cfg = frappe.get_doc("CRM Intake Form", intake_form)
	return {
		# The Web Form NAME (autonamed off the title), never the route — they coincide by accident.
		"web_form": builder.web_form_name_for(cfg),
		"published": _published(cfg),
		"route": cfg.route,
		"reasons": builder.readiness(cfg),
		"grain": {
			"vertical": cfg.custom_vertical,
			"group": cfg.custom_group,
			"program": cfg.custom_current_program,
			"source": cfg.source,
		},
	}


def _published(cfg) -> bool:
	"""Is this form's Web Form live? One source, already indexed — no shadow flag."""
	wf_name = builder.web_form_name_for(cfg)
	return bool(wf_name and frappe.db.get_value("Web Form", wf_name, "published"))
