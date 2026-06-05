"""Render the Stage fields + the patient-footprint field in the CRM Lead side panel.

The side-panel layout is a `CRM Fields Layout` record (`CRM Lead-Side Panel`), whose
`layout` field is a JSON array of sections -> columns -> fields. We ensure a
"Lead Status" section carries the Stage fields and a "Patient Footprint" section
carries the related-programs awareness field, without disturbing the rest of the
panel. Idempotent (skips fields already present) and soft-fail (if the record/shape
is absent, log and continue — never error).

NOTE FOR THE APPLY AGENT: this edits a DB-only layout record. If the live
`CRM Lead-Side Panel` JSON shape differs from the section/column/field shape assumed
here, this patch leaves the panel untouched and logs — verify the panel after migrate
and, if the fields didn't land, add them via the layout editor (then capture the
layout as a fixture in Phase 3).
"""
import json

import frappe

LAYOUT_NAME = "CRM Lead-Side Panel"
SECTION_LABEL = "Lead Status"
# Single combined Stage pick + the derived (read-only) Main Stage.
STAGE_FIELDS = ["custom_stage", "custom_main_stage"]
# Phase 6 — patient awareness across grain-split leads (its own panel section).
FOOTPRINT_SECTION = "Patient Footprint"
FOOTPRINT_FIELDS = ["custom_patient_other_programs"]
# Native lifecycle fields we DON'T use — Stage carries the lifecycle. The native
# status pill is DOM-hidden separately; status stays defaulted to "New" in the
# background (required field, so we never remove the field — only its panel slot).
# custom_substage is the retired two-field model — strip it if an old layout has it.
REMOVE_FIELDS = ["status", "lost_reason", "lost_notes", "custom_substage"]


def _fields_in(layout):
	"""Flatten every fieldname already referenced anywhere in the layout."""
	seen = set()
	for section in layout:
		for column in section.get("columns", []) or []:
			for f in column.get("fields", []) or []:
				name = f if isinstance(f, str) else f.get("fieldname")
				if name:
					seen.add(name)
	return seen


def execute():
	if not frappe.db.exists("CRM Fields Layout", LAYOUT_NAME):
		frappe.log_error(f"{LAYOUT_NAME} not found; skipped stage side-panel inject", "add_stage_to_side_panel")
		return

	doc = frappe.get_doc("CRM Fields Layout", LAYOUT_NAME)
	try:
		layout = json.loads(doc.layout) if doc.layout else []
	except (ValueError, TypeError):
		frappe.log_error(f"{LAYOUT_NAME}.layout not JSON; skipped", "add_stage_to_side_panel")
		return

	if not isinstance(layout, list):
		frappe.log_error(f"{LAYOUT_NAME}.layout not a section list; skipped", "add_stage_to_side_panel")
		return

	changed = False

	# 1. strip native lifecycle fields from every section (stage/substage replace them)
	for section in layout:
		for column in section.get("columns", []) or []:
			fields = column.get("fields", []) or []
			kept = [f for f in fields if (f if isinstance(f, str) else f.get("fieldname")) not in REMOVE_FIELDS]
			if len(kept) != len(fields):
				column["fields"] = kept
				changed = True

	# 2. ensure each section carries its fields (Stage in "Lead Status", awareness in "Patient Footprint")
	def ensure(section_label, fields):
		missing = [f for f in fields if f not in _fields_in(layout)]
		if not missing:
			return False
		target = None
		for section in layout:
			if (section.get("label") or "").strip() == section_label:
				target = section
				break
		if target is None:
			target = {"label": section_label, "columns": [{"fields": []}]}
			layout.insert(0, target)
		if not target.get("columns"):
			target["columns"] = [{"fields": []}]
		target["columns"][0].setdefault("fields", [])
		target["columns"][0]["fields"].extend(missing)
		return True

	if ensure(SECTION_LABEL, STAGE_FIELDS):
		changed = True
	if ensure(FOOTPRINT_SECTION, FOOTPRINT_FIELDS):
		changed = True

	if not changed:
		return
	doc.layout = json.dumps(layout)
	doc.save(ignore_permissions=True)
	frappe.db.commit()
