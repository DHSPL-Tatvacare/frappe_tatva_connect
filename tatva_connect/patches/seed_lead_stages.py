"""Seed per-program lead lifecycles into CRM Lead Stage. GENERIC: add a program by
adding an entry to PROGRAMS — no code change beyond the data.

Model: a main stage is a row with substage_of blank; a substage is a row whose
substage_of points at its main stage. `selectable` marks the rows a lead can pick
(the leaves): every substage, plus any main stage that has no substages. The Lead's
single `custom_stage` Link filters on selectable=1, so ops picks one leaf and the
main stage is derived. Idempotent: get-or-create keyed on (program, stage, substage_of).
"""
import frappe

# program -> [(main_stage, [substages...]), ...]. Empty list = a standalone main stage.
PROGRAMS = {
	"Niva Bupa": [
		("New", ["New Lead", "Ringing", "Call Back"]),
		("Opportunity", ["Payment Link Sent", "Details Shared", "Need time to decide"]),
		(
			"Lost",
			[
				"Price Concerns",
				"Not Comfortable with Online Consultation",
				"Already have an expert",
				"Will manage on his own",
				"Wants More Sessions",
				"Lost to a competitor",
				"Not ready to share the details",
				"Duplicate Lead",
				"No Reason Given",
				"Trust issue about the report",
				"Others",
			],
		),
		("Archived", ["RNR after 3 attempts", "3 Attempts after pitching"]),
		("Converted", []),
	],
}


def _get_or_create(program, stage, substage_of, selectable, position):
	name = frappe.db.get_value(
		"CRM Lead Stage",
		{"program": program, "stage": stage, "substage_of": substage_of or ""},
		"name",
	)
	if name:
		# keep selectable in sync on re-run (e.g. a main gains/loses substages)
		if frappe.db.get_value("CRM Lead Stage", name, "selectable") != selectable:
			frappe.db.set_value("CRM Lead Stage", name, "selectable", selectable)
		return name
	doc = frappe.get_doc(
		{
			"doctype": "CRM Lead Stage",
			"program": program,
			"stage": stage,
			"substage_of": substage_of,
			"selectable": selectable,
			"position": position,
		}
	).insert(ignore_permissions=True)
	return doc.name


def execute():
	for program, stages in PROGRAMS.items():
		if not frappe.db.exists("CRM Program", program):
			frappe.log_error(f"CRM Program '{program}' missing; skipped its lead-stage seed", "seed_lead_stages")
			continue
		for s_idx, (stage, substages) in enumerate(stages):
			# a main stage is selectable only when it has no substages
			main_name = _get_or_create(program, stage, None, 0 if substages else 1, s_idx)
			for sub_idx, substage in enumerate(substages):
				_get_or_create(program, substage, main_name, 1, sub_idx)
	frappe.db.commit()
