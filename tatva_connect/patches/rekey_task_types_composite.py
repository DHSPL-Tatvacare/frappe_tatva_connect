"""Re-key CRM Task Type from name-keyed (`type_name`) to grain-scoped composite keys
`vertical::group::program::type_name` (invariant 7 / ADR-grain-scoped-task-types), so the SAME
type_name can carry a different schema per grain. The grain moves from the `scope` child table onto the
parent record (Link fields + composite autoname) — bringing task types in line with CRM Picklist Value /
CRM Lead Stage. Idempotent, forward-only.

Per record (by its scope rows):
  * 1 row (every active type today): set the parent grain → `rename_doc` (cascades the `custom_task_type`
    Link on every CRM Task automatically). Clean 1:1.
  * 0 rows (dormant): left name-keyed; the new resolution requires a vertical, so a blank-grain type
    never appears in a lead's picker (stays dormant) — no change of behaviour.
  * >1 rows: a 1→N split (one composite record per grain + reassign tasks by their lead grain) — none
    exist today (each type has a single scope row); logged + skipped so prod handles it deliberately,
    never mis-migrated.

Display names are PRESERVED (the existing distinct names like "Ujvira Welcome Call" stay as `type_name`);
curating them to clean names ("Welcome Call") is a separate db-seeds step, not this structural re-key.
"""
import frappe

TT = "CRM Task Type"


def _composite(vertical, group, program, type_name):
	return "{0}::{1}::{2}::{3}".format(vertical or "", group or "", program or "", type_name)


def execute():
	for name in [r.name for r in frappe.get_all(TT, fields=["name"])]:
		if "::" in name:
			continue  # already composite — idempotent
		scope = frappe.get_all(
			"CRM Task Type Scope",
			filters={"parent": name, "parenttype": TT},
			fields=["vertical", "`group` as grp", "program"],
		)
		if not scope:
			continue  # dormant (no grain) — leave name-keyed; new resolution skips blank-vertical types
		if len(scope) > 1:
			frappe.log_error(
				"CRM Task Type '{0}' has {1} scope rows — needs a manual 1->N composite split (ADR).".format(
					name, len(scope)
				),
				"rekey_task_types_composite",
			)
			continue
		s = scope[0]
		new = _composite(s.vertical, s.grp, s.program, name)
		if frappe.db.exists(TT, new):
			continue
		# Set the parent grain so it's consistent with the composite key (autoname reads these fields).
		frappe.db.set_value(
			TT, name, {"vertical": s.vertical, "group": s.grp, "program": s.program}, update_modified=False
		)
		frappe.rename_doc(TT, name, new, force=True)
	frappe.db.commit()
