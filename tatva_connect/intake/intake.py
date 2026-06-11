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

	# M-3: resolve on the canonical lead grain — mobile + vertical + group — using the
	# form's FORCED routing (cfg). A find-or-create on phone ALONE would hijack an
	# existing lead on a different (line, group); instead, a different trio yields a
	# SECOND lead and the existing lead's routing is never overwritten.
	name = frappe.db.get_value(
		"CRM Lead",
		{
			"mobile_no": mobile,
			"custom_vertical": cfg.get("custom_vertical"),
			"custom_group": cfg.get("custom_group"),
		},
		"name",
	)
	lead = frappe.get_doc("CRM Lead", name) if name else frappe.new_doc("CRM Lead")
	lead.mobile_no = mobile
	lead.status = lead.status or "New"

	# Routing is set on CREATE only. On a matched lead we must NEVER rewrite its
	# vertical/group/program — finding by the trio already guarantees vertical+group
	# match; program is a mutable attribute transitioned deliberately, not by a form.
	if not name:
		for f in _ROUTING:
			if cfg.get(f):
				lead.set(f, cfg.get(f))
		# Provenance (hygiene rule 8): stamp which intake form created this lead.
		if not (lead.get("custom_source_origin") or "").strip():
			lead.custom_source_origin = "Intake form: {0}".format(cfg.name)

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


# Pick-only reference masters — NEVER auto-created from a form. The form PICKS from the
# curated, grain-scoped masters (loaded via runbook SQL); a typed/"not listed" value is
# still recorded as text on the lead, but never births a master row. Doctor/Hospital are
# now first-class grain-scoped masters (vertical::group::program key, Doctor->Hospital FK),
# so free-form auto-add is gone — they'd need a grain the form can't safely infer per row.
_PICK_ONLY_MASTERS = {"CRM City", "CRM Doctor", "CRM Hospital"}


def _resolve_value(doc, m):
	"""Pick value, else the manual companion. If manual is used and a master is
	configured, auto-add that master (normalized, match-or-create) so it joins the
	pick-list next time — unless it's a pick-only master (City)."""
	picked = (doc.get(m.source_field) or "").strip()
	manual = (doc.get(m.manual_field) or "").strip() if m.manual_field else ""

	# "manual wins" when nothing was picked, or the pick is an explicit Other sentinel
	if manual and (not picked or picked == "Others" or picked == "Other"):
		if m.master_doctype:
			# The display field the value lands in is the part after the target prefix
			# (e.g. care:doctor_name -> doctor_name). Derived, not stored on the map.
			display_field = (m.target or "").partition(":")[2] or None
			canonical = _ensure_master(m.master_doctype, display_field, manual)
			return canonical or manual
		return manual
	# A picked value from a Link field is the row's PK — the composite key now
	# (e.g. "GF Care::Anaya::Nivolumab::Apollo", or City's "Bengaluru::Karnataka").
	# Store the HUMAN label (the link target's title_field), never the opaque key.
	# Non-Link sources (Select / Data) pass straight through.
	return _link_label(doc, m.source_field, picked) if picked else picked


def _link_label(doc, source_field, value):
	"""If source_field is a Link, resolve the picked PK to the linked row's title
	(its title_field, else `name`). Any non-Link field returns the value unchanged."""
	df = frappe.get_meta(doc.doctype).get_field(source_field)
	if not df or df.fieldtype != "Link" or not df.options:
		return value
	title_field = frappe.get_meta(df.options).get("title_field") or "name"
	return frappe.db.get_value(df.options, value, title_field) or value


def _ensure_master(doctype, display_field, value):
	"""Match-or-create a master on the NORMALIZED display value (case-insensitive).

	* Never auto-creates a pick-only master (City) — returns the raw value so the
	  lead still records what was typed, but no junk master row is born.
	* Matches an EXISTING row whose normalized display value equals the normalized
	  input (so "Apollo "/"apollo"/"Apollo" all reuse one row); else inserts one.
	* Returns the canonical display value to store on the lead.
	"""
	from tatva_connect.taxonomy.normalize import normalize_display

	if not display_field:
		return value
	canonical = normalize_display(value)
	if not canonical:
		return value

	# Exact match on the normalized display value (not on opaque `name`). Both stored
	# and input values are normalize_display'd, so '=' is exact AND case-insensitive
	# (DB collation) — and avoids a LIKE treating a literal % / _ in a name as a wildcard.
	existing = frappe.get_all(
		doctype, filters={display_field: canonical}, fields=[display_field], limit=1
	)
	if existing:
		return existing[0].get(display_field)

	if doctype in _PICK_ONLY_MASTERS:
		# Pick-only: do not grow from a form. Keep the typed value on the lead.
		return canonical

	d = frappe.new_doc(doctype)
	d.set(display_field, canonical)
	# Governed growth (Phase 3): flag form-born rows for ops review/merge.
	if frappe.get_meta(doctype).has_field("review_pending"):
		d.set("review_pending", 1)
	d.insert(ignore_permissions=True)
	return d.get(display_field)


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
