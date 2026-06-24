"""Generic web-intake processor — config-driven, reused by every enrolment form.

A Web Form lands a row in a staging doctype (e.g. CRM Enrolment Submission). On
insert, this turns that row into a routed, deduped CRM Lead using the linked
`CRM Intake Form` config: fixed routing + a field map. Adding a future form needs
only a new Web Form + a new CRM Intake Form row — no new Python.
"""
import re

import frappe
from frappe import _

from tatva_connect import automation

_TABLE = {
	"plan": "custom_plan_profile",
	"care": "custom_care_providers_profile",
	"lab": "custom_lab_profile",
	# The drug-program child holds Nivolumab dosage/indication (nivo_dosage / nivo_indication).
	# Its absence here is the live data-loss bug — a `drug:*` mapping had nowhere to land.
	"drug": "custom_drug_program_profile",
}
_ROUTING = ("source", "custom_vertical", "custom_group", "custom_current_program", "custom_origin_vertical")

# The back-link the per-form submission row carries to its contract (set by the builder).
_INTAKE_FORM_FIELD = "intake_form"


def target_doctype(target_table):
	"""Resolve a mapping's target_table to the DOCTYPE whose fields it writes:
	`lead` -> CRM Lead; a child table -> the child doctype (the CRM Lead Table field's
	`options`, NOT the fieldname); `note` / unknown -> None. ONE resolver — the save-time
	validation AND the builder field-discovery both call this, so a child-table fieldname
	(`custom_drug_program_profile`) can never again be mistaken for a doctype."""
	table = (target_table or "").strip()
	if table == "lead":
		return "CRM Lead"
	cf = _TABLE.get(table)
	if not cf:
		return None
	df = frappe.get_meta("CRM Lead").get_field(cf)
	return df.options if df else None


def _normalize_phone(raw: str) -> str:
	digits = re.sub(r"\D", "", raw or "")
	if len(digits) == 10:
		digits = "91" + digits
	return "+" + digits


_INTAKE_DOCTYPES_CACHE_KEY = "tatva_connect:intake_doctypes"


def _intake_doctypes() -> set:
	"""The DocType names that are an enabled intake form's submission sink — the cheap
	guard set the wildcard router checks. Memoised; busted on any CRM Intake Form write
	(and on every sync_form). Names are DERIVED from the form name via the builder, so
	there is no stored column to read."""
	cached = frappe.cache().get_value(_INTAKE_DOCTYPES_CACHE_KEY)
	if cached is None:
		from tatva_connect.intake.builder import safe_doctype_name_for

		cached = set()
		# The wildcard fires site-wide, incl. during install before the contract table exists.
		if frappe.db.table_exists("CRM Intake Form"):
			for cfg in frappe.get_all("CRM Intake Form", filters={"enabled": 1}, fields=["name", "form_name"]):
				dt = safe_doctype_name_for(frappe._dict(cfg))  # None for a legacy/invalid name
				if dt and frappe.db.exists("DocType", dt):
					cached.add(dt)
		frappe.cache().set_value(_INTAKE_DOCTYPES_CACHE_KEY, cached)
	return cached


def bust_intake_doctype_cache(doc=None, method=None):
	"""Drop the memoised intake-doctype guard set. doc_events hook on CRM Intake Form
	(on_update/on_trash) AND called by builder.sync_form, so the wildcard router's guard
	never serves a stale set after a form is added, toggled, or scaffolded."""
	frappe.cache().delete_value(_INTAKE_DOCTYPES_CACHE_KEY)


def route_submission(doc, method=None):
	"""Wildcard after_insert (doc_events["*"]) — the ONE brain for EVERY per-form intake
	sink. Fires site-wide, so it early-returns cheaply for any doctype that is not an
	enabled intake form's submission table (a single cached set membership test). Runtime
	per-form doctypes can't carry their own code hooks; the wildcard is the native,
	single-brain way to process them. On a hit it runs the same fold as process_submission."""
	if doc.doctype not in _intake_doctypes():
		return
	if not automation.is_enabled("Lead::Enrolment::intake"):
		return
	if not doc.get(_INTAKE_FORM_FIELD):
		return
	cfg = frappe.get_cached_doc("CRM Intake Form", doc.get(_INTAKE_FORM_FIELD))
	if not cfg.enabled:
		return
	_fold_submission_to_lead(doc, cfg)


def process_submission(doc, method=None):
	"""after_insert on the CRM Enrolment Submission staging doctype -> upsert the lead.

	The legacy shared-staging path. Resolves the contract then hands off to the ONE fold
	(_fold_submission_to_lead) — the SAME body the wildcard router runs, so there is one
	implementation, never a copy.
	"""
	if not automation.is_enabled("Lead::Enrolment::intake"):
		return
	if doc.processed or not doc.intake_form:
		return
	cfg = frappe.get_cached_doc("CRM Intake Form", doc.intake_form)
	if not cfg.enabled:
		return
	_fold_submission_to_lead(doc, cfg)


def _fold_submission_to_lead(doc, cfg):
	"""The ONE fold: a resolved submission row + its contract -> a routed CRM Lead via the
	partner brain. Shared by BOTH process_submission (CRM Enrolment Submission) and the
	wildcard route_submission (per-form runtime doctypes) — one implementation, no copy.

	Intake is a SECOND SOURCE feeding the ONE lead-create brain (partner._upsert_one):
	the form is resolved into a partner-shaped payload + a grain descriptor, then handed
	to the brain — which owns find-or-create, forced routing, program resolution, child
	upsert, dedup and the ignore_permissions write. Intake keeps only its own pre-step
	(value resolution + master match) and post-step (notes, prescription, stamp).
	"""
	from tatva_connect.api.partner import _upsert_one

	# Public form: never surface internal notices (e.g. assignment's "Shared with
	# … Read access") to the patient. Request-scoped; auto-resets next request.
	frappe.flags.mute_messages = True

	# Resolve the form into a partner-shaped payload using intake's OWN field logic.
	# parent_fields / child_allow are intake's tight allowlist — exactly the form's
	# mapped targets (§2c) — built here from cfg.mappings, NOT the partner catalog.
	item = {"mobile_no": doc.get("phone")}  # RAW phone; the brain's _norm_phone + doc_events canonicalise
	parent_fields = []
	child_allow = {}
	notes = []
	for m in cfg.mappings:
		val = _resolve_value(doc, m)
		if not val:
			continue
		# ONE target representation: the structured (target_table, target_field) pair.
		table = (m.target_table or "").strip()
		field = (m.target_field or "").strip()
		if table == "lead":
			item[field] = val
			parent_fields.append(field)
		elif table in _TABLE:
			cf = _TABLE[table]
			item.setdefault(cf, [{}])[0][field] = val
			child_allow.setdefault(cf, []).append(field)
		elif table == "note":
			notes.append((field or "Note", val))

	# Provenance (latest-source-wins): stamp which intake form sourced this lead. Sent on
	# every upsert; the brain's doc.update(parent) applies it on update too — so the lead
	# always reflects its most recent source (see Phase 0 §provenance decision).
	if cfg.get("custom_origin_vertical"):
		item["custom_origin_vertical"] = cfg.get("custom_origin_vertical")
		parent_fields.append("custom_origin_vertical")
	item["custom_source_origin"] = "Intake form: {0}".format(cfg.name)
	parent_fields.append("custom_source_origin")

	# Grain descriptor — quacks like a CRM Lead API Mapping. The brain reads ONLY
	# .source/.vertical/.crm_group/.program off mp (verified in _force_routing/_resolve_program),
	# never a mapping's DB identity, so this _dict is a complete substitute.
	mp = frappe._dict(
		source=cfg.source,
		vertical=cfg.custom_vertical,
		crm_group=cfg.custom_group,
		program=cfg.custom_current_program,
	)

	# is_sysmgr=False + mp set => _collect's allow_routing is False: submitter-sent routing
	# is dropped, grain is forced from mp (§2c). allowed_programs=[] is unconsulted for a
	# forced-program form (program_mode.resolve_program returns the pin before reading it).
	doc_lead, _action = _upsert_one(
		item, mp, False, parent_fields, child_allow, allowed_programs=[]
	)

	for title, content in notes:
		frappe.get_doc(
			{
				"doctype": "FCRM Note",
				"title": title,
				"content": content,
				"reference_doctype": "CRM Lead",
				"reference_docname": doc_lead.name,
			}
		).insert(ignore_permissions=True)

	_attach_prescription(doc, doc_lead.name)

	# Stamp the result back on the submission — but only on a doctype that carries these
	# result fields (the CRM Enrolment Submission staging table). A per-form runtime sink
	# may not, so guard each set on field existence (no stamp != failed processing).
	meta = doc.meta
	if meta.has_field("lead"):
		doc.db_set("lead", doc_lead.name, update_modified=False)
	if meta.has_field("processed"):
		doc.db_set("processed", 1, update_modified=False)


def _process_submission_legacy(doc, method=None):
	"""after_insert on the staging doctype -> upsert the CRM Lead.

	LEGACY (Phase 0 spike): the pre-fold processor, kept verbatim for the parity test
	(test_intake_brain_parity) to byte-compare against the new partner-brain path.
	Deleted in Phase 0 finalize once parity is GREEN.
	"""
	if not automation.is_enabled("Lead::Enrolment::intake"):
		return
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
		note = _apply_target(lead, m.target_table, m.target_field, val)
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
			# The display field the value lands in is the mapping's target_field
			# (e.g. care / doctor_name). Read from the structured target.
			display_field = (m.target_field or "").strip() or None
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


def _apply_target(lead, table, field, val):
	"""Structured target: table in (lead|plan|care|lab|drug|note), field = the destination
	fieldname (or the Note title for `note`). Returns (title, content) for note targets
	(created after the lead is saved). Legacy comparator path only — the live fold inlines
	this same dispatch (one representation, read both places)."""
	table = (table or "").strip()
	field = (field or "").strip()
	if table == "lead":
		lead.set(field, val)
	elif table in _TABLE:
		rows = lead.get(_TABLE[table])
		row = rows[0] if rows else lead.append(_TABLE[table], {})
		row.set(field, val)
	elif table == "note":
		return (field or "Note", val)
	return None


def _attach_prescription(doc, lead_name):
	"""The Attach field already stored a File against the submission; also surface
	it on the lead so it shows in the lead's attachments."""
	url = doc.get("prescription")
	if not url:
		return
	from tatva_connect.storage import file_manager

	file_manager.link(url, attached_to_doctype="CRM Lead", attached_to_name=lead_name)
