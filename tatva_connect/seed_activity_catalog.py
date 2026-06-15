"""TatvaPractice field-sales activity-type catalog — OPERATOR-RUN, idempotent.

This is BUSINESS config DATA (invariant #5): it is NOT wired into after_migrate/hooks.
The operator runs it by hand AFTER the masters exist, then activity types appear in the
Phase A "Log Activity" picker for the configured grain.

It upserts ~20 TatvaPractice activity types as CRM Task Type rows the engine already
understands: visit_mode (derived from the name), the per-type form schema (CRM Task Type
Field, with first_class_target on the four mapped columns), and the grain scope
(CRM Task Type Scope). Re-running rebuilds the same rows (idempotent).

PREREQUISITES (must exist first, or the scope/Link rows won't resolve):
  • CRM Vertical  = SCOPE_VERTICAL   (default "TatvaPractice")
  • CRM Group     = SCOPE_GROUP      (default "Sales")
  • CRM Program   = SCOPE_PROGRAM    (default "FieldSales")
  Blank any of the three below for a wildcard axis (e.g. SCOPE_PROGRAM = "" => all programs
  under TatvaPractice/Sales).

RUN (operator, after the bench/docker upgrade — example for the dev site):
  bench --site dev.localhost execute tatva_connect.seed_activity_catalog.seed

NOTE: each type's `_src` marks whether its fields are transcribed from an LSQ screen
("screenshot") or built from the documented recurring-field pattern ("pattern"); pattern
types are a safe starting point — confirm/adjust them as plain config in the UI.
"""
import frappe

# === OPERATOR: confirm these match your real master names before running. ===
SCOPE_VERTICAL = "TatvaPractice"
SCOPE_GROUP = "Sales"
SCOPE_PROGRAM = "FieldSales"


# -- field builders (first_class_target set ONLY on the four mapped columns) --
def _f(label, fieldname, fieldtype, options="", reqd=0, fc=""):
	return {"label": label, "fieldname": fieldname, "fieldtype": fieldtype,
			"options": options, "reqd": reqd, "first_class_target": fc}


def _notes():
	return _f("Notes", "notes", "Small Text")


def _status(label, options):
	return _f(label, "visit_status", "Select", options, reqd=1, fc="custom_visit_status")


def _next_visit():
	return _f("Next Visit Date Time", "next_visit_at", "Datetime", fc="custom_next_visit_at")


def _reschedule():
	return _f("Reschedule Date Time", "reschedule_at", "Datetime")


def _dropped():
	return _f("Dropped Reasons", "dropped_reasons", "Small Text")


def _asm_along():
	return _f("Was the ASM along with you?", "asm_along", "Check")


def _select_asm():
	return _f("Select ASM", "asm", "Link", "User", fc="custom_asm")


def _demo():
	return _f("Demo Scheduled Date Time", "demo_scheduled_at", "Datetime", fc="custom_demo_scheduled_at")


def _followup():
	return _f("Visit Follow up Date Time", "visit_followup_at", "Datetime")


# -- per-family schema bundles ------------------------------------------------
def _visit_schema(status_label, status_options):
	return [_notes(), _status(status_label, status_options), _next_visit(),
			_reschedule(), _dropped(), _asm_along(), _select_asm()]


def _intro_schema():
	return [
		_status("Visit Completed - Next Steps", "Schedule Demo\nReschedule Visit\nDropped"),
		_f("Visit Not Done - Next Steps", "visit_not_done_next_steps", "Select", "Reschedule Visit\nDropped"),
		_followup(), _dropped(), _demo(), _asm_along(), _select_asm(),
	]


_VISIT_OPTS = "Visit Completed\nVisit Rescheduled\nDropped"
_GENERIC_OPTS = "Completed\nRescheduled\nDropped"

# (type name, schema rows, source). visit_mode is derived from the name (see _visit_mode).
TYPES = [
	# --- transcribed from the LSQ activity-type screens ---
	("Contact Type", [_notes(), _f("Select Type Of Contact", "contact_type", "Select", "Phone Call\nPhysical Visit")], "screenshot"),
	("Courtesy Visit Field Visit", _visit_schema("Visit Status", _VISIT_OPTS), "screenshot"),
	("Courtesy Visit Phone Call", _visit_schema("Visit Status", _VISIT_OPTS), "screenshot"),
	("Doctor Activation Field Visit", _visit_schema("Doctor Activation Status", "Reschedule\nCompleted\nDropped"), "screenshot"),
	("Doctor Activation Phone Call", _visit_schema("Doctor Activation Status", "Reschedule\nCompleted\nDropped"), "screenshot"),
	("Introductory Meeting", _intro_schema(), "screenshot"),
	("Introductory Meeting Phone Call", _intro_schema(), "screenshot"),
	("Introductory Meeting Physical Visit", _intro_schema(), "screenshot"),
	# --- built from the documented recurring-field pattern (confirm in UI) ---
	("Demo Scheduled Status Field Visit", _visit_schema("Visit Status", _GENERIC_OPTS) + [_demo()], "pattern"),
	("Demo Scheduled Status Phone Call", _visit_schema("Visit Status", _GENERIC_OPTS) + [_demo()], "pattern"),
	("Doctor Training Field Visit", _visit_schema("Visit Status", _GENERIC_OPTS), "pattern"),
	("Doctor Training Phone Call", _visit_schema("Visit Status", _GENERIC_OPTS), "pattern"),
	("Onboarding Status Field Visit", _visit_schema("Visit Status", _GENERIC_OPTS), "pattern"),
	("Onboarding Status Phone Call", _visit_schema("Visit Status", _GENERIC_OPTS), "pattern"),
	("Manager Visit", [_notes(), _status("Visit Status", _GENERIC_OPTS), _next_visit(), _dropped(), _asm_along(), _select_asm()], "pattern"),
	# --- system / popup log types: minimal note-only form (confirm in UI) ---
	("Document Generation", [_notes()], "pattern"),
	("Lead Shared through Agent Popup", [_notes()], "pattern"),
	("Lead updated", [_notes()], "pattern"),
	("Opportunity Shared through Agent Popup", [_notes()], "pattern"),
	("Sales Activity", [_notes()], "pattern"),
]


def _visit_mode(name):
	"""Name rule: '... Field Visit'/'... Physical Visit' => In-Person; '... Phone Call' => Phone; else None."""
	if name.endswith("Field Visit") or name.endswith("Physical Visit"):
		return "In-Person"
	if name.endswith("Phone Call"):
		return "Phone"
	return "None"


def seed():
	scope = {"vertical": SCOPE_VERTICAL, "group": SCOPE_GROUP, "program": SCOPE_PROGRAM}
	for name, schema, _src in TYPES:
		doc = (frappe.get_doc("CRM Task Type", name) if frappe.db.exists("CRM Task Type", name)
			   else frappe.new_doc("CRM Task Type"))
		if doc.is_new():
			doc.type_name = name
		doc.visit_mode = _visit_mode(name)
		doc.is_logged_complete = 1
		doc.set("schema", [])
		for f in schema:
			doc.append("schema", f)
		doc.set("scope", [dict(scope)])
		doc.save(ignore_permissions=True)
	frappe.db.commit()
	print("Seeded {0} activity types into grain {1}".format(len(TYPES), scope))
