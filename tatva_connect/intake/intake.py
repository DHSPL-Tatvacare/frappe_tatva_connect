"""Generic web-intake processor — config-driven, reused by every enrolment form.

A Web Form lands a row in a staging doctype (e.g. CRM Enrolment Submission). On
insert, this turns that row into a routed, deduped CRM Lead using the linked
`CRM Intake Form` config: fixed routing + a field map. Adding a future form needs
only a new Web Form + a new CRM Intake Form row — no new Python.
"""
import re

import frappe
from frappe import _

_TABLE = {
	"plan": "custom_plan_profile",
	"care": "custom_care_providers_profile",
	"lab": "custom_lab_profile",
}
_ROUTING = ("source", "custom_vertical", "custom_group", "custom_current_program", "custom_origin_vertical")


def _normalize_phone(raw: str) -> str:
	digits = re.sub(r"\D", "", raw or "")
	if len(digits) == 10:
		digits = "91" + digits
	return "+" + digits


def process_submission(doc, method=None):
	"""after_insert on the staging doctype -> upsert the CRM Lead."""
	if doc.processed or not doc.intake_form:
		return
	cfg = frappe.get_cached_doc("CRM Intake Form", doc.intake_form)
	if not cfg.enabled:
		return

	# Public form: never surface internal notices (e.g. assignment's "Shared with
	# … Read access") to the patient. Request-scoped; auto-resets next request.
	frappe.flags.mute_messages = True

	mobile = _normalize_phone(doc.get("phone"))
	name = frappe.db.get_value("CRM Lead", {"mobile_no": mobile}, "name")
	lead = frappe.get_doc("CRM Lead", name) if name else frappe.new_doc("CRM Lead")
	lead.mobile_no = mobile
	lead.status = lead.status or "New"

	for f in _ROUTING:
		if cfg.get(f):
			lead.set(f, cfg.get(f))

	notes = []
	for m in cfg.mappings:
		val = _resolve_value(doc, m)
		if not val:
			continue
		note = _apply_target(lead, m.target, val)
		if note:
			notes.append(note)

	if not (lead.first_name or "").strip():
		lead.first_name = "(no name)"

	lead.save(ignore_permissions=True) if name else lead.insert(ignore_permissions=True)

	for title, content in notes:
		frappe.get_doc(
			{
				"doctype": "FCRM Note",
				"title": title,
				"content": content,
				"reference_doctype": "CRM Lead",
				"reference_docname": lead.name,
			}
		).insert(ignore_permissions=True)

	_attach_prescription(doc, lead.name)

	doc.db_set("lead", lead.name, update_modified=False)
	doc.db_set("processed", 1, update_modified=False)


def _resolve_value(doc, m):
	"""Pick value, else the manual companion. If manual is used and a master is
	configured, auto-create that master record so it joins the pick-list next time."""
	picked = (doc.get(m.source_field) or "").strip()
	manual = (doc.get(m.manual_field) or "").strip() if m.manual_field else ""

	# "manual wins" when nothing was picked, or the pick is an explicit Other sentinel
	if manual and (not picked or picked == "Others" or picked == "Other"):
		if m.master_doctype:
			_ensure_master(m.master_doctype, manual)
		return manual
	return picked


def _ensure_master(doctype, value):
	if frappe.db.exists(doctype, value):
		return
	autoname = frappe.get_meta(doctype).autoname or ""
	field = autoname.split(":", 1)[1] if autoname.startswith("field:") else None
	d = frappe.new_doc(doctype)
	if field:
		d.set(field, value)
	d.insert(ignore_permissions=True)


def _apply_target(lead, target, val):
	"""target = lead:field | plan:field | care:field | lab:field | note:Title.
	Returns (title, content) for note targets (created after the lead is saved)."""
	prefix, _, field = (target or "").partition(":")
	if prefix == "lead":
		lead.set(field, val)
	elif prefix in _TABLE:
		rows = lead.get(_TABLE[prefix])
		row = rows[0] if rows else lead.append(_TABLE[prefix], {})
		row.set(field, val)
	elif prefix == "note":
		return (field or "Note", val)
	return None


def _attach_prescription(doc, lead_name):
	"""The Attach field already stored a File against the submission; also surface
	it on the lead so it shows in the lead's attachments."""
	url = doc.get("prescription")
	if not url:
		return
	frappe.get_doc(
		{
			"doctype": "File",
			"file_url": url,
			"attached_to_doctype": "CRM Lead",
			"attached_to_name": lead_name,
			"is_private": 1,
		}
	).insert(ignore_permissions=True)
