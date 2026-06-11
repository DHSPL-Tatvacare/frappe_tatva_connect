"""One-time rename of existing rows to the P8 naming standard (audit #8/#9/#10).

These doctypes were created with `autoname: hash`; P8 switches them to composite
`format:` keys (routing tables, checklist template, lead stage) and a `naming_series`
(enrolment submission). Existing rows keep their hash names until renamed. We use
`frappe.rename_doc` — NEVER delete+recreate — so every Link that points at a row is
re-pointed atomically (critically CRM Lead.custom_stage and CRM Lead Stage.substage_of).

Idempotent: a row already at its target name is skipped; a row whose target name is
already taken by another row is skipped + logged (manual dedup), never overwritten.
"""
import frappe
from frappe.model.rename_doc import rename_doc

SEP = "::"


def _target_format(parts):
	# Canonicalise NULL/'' to one sentinel ('') before joining — matches the
	# controller _canonicalize so the patch and live writes agree on the name.
	return SEP.join((p or "").strip() for p in parts)


def _rename(doctype, old, new):
	if not new or new == old:
		return
	if frappe.db.exists(doctype, new):
		frappe.log_error(
			f"{doctype}: target name {new!r} already exists; left {old!r} as-is for manual dedup",
			"rename_to_naming_standard",
		)
		return
	# validate=False: the name is a pure key change; collisions are checked above and
	# the row already passed validation. Avoids re-running the dup-guard mid-rename.
	rename_doc(doctype, old, new, force=True, validate=False, show_alert=False, rebuild_search=False)


def _rename_routing(doctype):
	for r in frappe.get_all(doctype, fields=["name", "vertical", "psp_group", "program"]):
		_rename(doctype, r.name, _target_format([r.vertical, r.psp_group, r.program]))


def _rename_checklist():
	dt = "CRM Task Checklist Template"
	for r in frappe.get_all(dt, fields=["name", "task_type", "vertical", "psp_group", "program"]):
		_rename(dt, r.name, _target_format([r.task_type, r.vertical, r.psp_group, r.program]))


def _rename_lead_stages():
	dt = "CRM Lead Stage"
	rows = frappe.get_all(dt, fields=["name", "program", "stage", "substage_of"])
	# Rename main stages (substage_of blank) first so the children's substage_of Link
	# is re-pointed to the parent's NEW name by the time we rename the children. (Even
	# if order slipped, rename_doc updates Link fields, but this keeps it obvious.)
	mains = [r for r in rows if not r.substage_of]
	subs = [r for r in rows if r.substage_of]
	for r in mains + subs:
		_rename(dt, r.name, _target_format([r.program, r.stage]))


def _rename_enrolment():
	from frappe.model.naming import make_autoname

	dt = "CRM Enrolment Submission"
	for r in frappe.get_all(dt, fields=["name", "creation"]):
		# Already on the series? skip.
		if (r.name or "").startswith("ENROL-"):
			continue
		doc = frappe.get_doc(dt, r.name)
		# Anchor the series year to the row's own creation date.
		new = make_autoname("ENROL-.YYYY.-.#####", doc=doc)
		_rename(dt, r.name, new)


def execute():
	for dt in ("CRM Acefone Account Routing", "CRM WATI Account Routing"):
		if frappe.db.exists("DocType", dt):
			_rename_routing(dt)
	if frappe.db.exists("DocType", "CRM Task Checklist Template"):
		_rename_checklist()
	if frappe.db.exists("DocType", "CRM Lead Stage"):
		_rename_lead_stages()
	if frappe.db.exists("DocType", "CRM Enrolment Submission"):
		_rename_enrolment()
	frappe.db.commit()

	# Post-rename assert: no CRM Lead.custom_stage may dangle (audit #10).
	if frappe.db.has_column("CRM Lead", "custom_stage"):
		dangling = frappe.db.sql(
			"""SELECT COUNT(*) FROM `tabCRM Lead`
			   WHERE IFNULL(custom_stage,'') != ''
			     AND custom_stage NOT IN (SELECT name FROM `tabCRM Lead Stage`)"""
		)[0][0]
		if dangling:
			frappe.throw(
				f"rename_to_naming_standard: {dangling} CRM Lead rows have a dangling "
				f"custom_stage after rename — aborting."
			)
