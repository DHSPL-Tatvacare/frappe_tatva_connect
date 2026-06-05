"""Repoint the dead `custom_psp_group` slot to `custom_group` in the CRM Lead layouts.

After the group field was renamed (custom_psp_group -> custom_group), the
`CRM Lead-Side Panel` and `CRM Lead-Quick Entry` layout records still referenced the
old fieldname, so the Routing panel showed a blank row. This rewrites every
`custom_psp_group` reference (whether a bare string or a `{"fieldname": ...}` dict)
to `custom_group`. Idempotent (no-op once repointed) and soft-fail (logs, never errors
if a record/shape is absent). Runs on migrate BEFORE the layout fixtures are captured,
so the exported side-panel/quick-entry fixtures carry custom_group, not the dead name.
"""
import json

import frappe

LAYOUT_NAMES = ["CRM Lead-Side Panel", "CRM Lead-Quick Entry"]
OLD = "custom_psp_group"
NEW = "custom_group"


def _repoint(layout):
	"""Walk sections -> columns -> fields; rewrite OLD -> NEW in place. Returns changed?"""
	changed = False
	for section in layout:
		for column in section.get("columns", []) or []:
			fields = column.get("fields", []) or []
			for i, f in enumerate(fields):
				if isinstance(f, str):
					if f == OLD:
						fields[i] = NEW
						changed = True
				elif isinstance(f, dict) and f.get("fieldname") == OLD:
					f["fieldname"] = NEW
					changed = True
	return changed


def execute():
	for name in LAYOUT_NAMES:
		if not frappe.db.exists("CRM Fields Layout", name):
			frappe.log_error(f"{name} not found; skipped group-slot repoint", "fix_group_layout_slot")
			continue

		doc = frappe.get_doc("CRM Fields Layout", name)
		try:
			layout = json.loads(doc.layout) if doc.layout else []
		except (ValueError, TypeError):
			frappe.log_error(f"{name}.layout not JSON; skipped", "fix_group_layout_slot")
			continue

		if not isinstance(layout, list):
			frappe.log_error(f"{name}.layout not a section list; skipped", "fix_group_layout_slot")
			continue

		if _repoint(layout):
			doc.layout = json.dumps(layout)
			doc.save(ignore_permissions=True)
	frappe.db.commit()
