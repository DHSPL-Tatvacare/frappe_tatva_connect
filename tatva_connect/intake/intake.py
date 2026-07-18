"""Generic web-intake processor — config-driven, reused by every enrolment form.

Each intake form has its OWN per-form runtime submission DocType (scaffolded by the builder from a
`CRM Intake Form` contract). A single wildcard after_insert (`route_submission`) turns a submitted
row into a routed, deduped CRM Lead using that contract: forced grain + a field map. Adding a form
needs only a new `CRM Intake Form` row — the builder makes its DocType + Web Form, no new Python.
"""
import frappe

from tatva_connect import automation

# The back-link the per-form submission row carries to its contract (set by the builder).
_INTAKE_FORM_FIELD = "intake_form"


def target_doctype(target_table):
	"""Resolve a mapping's target_table to the DOCTYPE whose fields it writes. Reads the ONE
	brain, `CRM Lead Section` — no private dict: `lead` -> CRM Lead, a child section -> its
	child doctype (the section row's own target_doctype). `note` / blank / not a section -> None.
	ONE resolver — the save-time validation AND the builder field-discovery both call this."""
	table = (target_table or "").strip()
	if not table or table == "note":
		return None
	if not frappe.db.exists("CRM Lead Section", table):
		return None
	return frappe.get_cached_doc("CRM Lead Section", table).target_doctype


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
	single-brain way to process them. On a hit it runs the fold below."""
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


def _fold_submission_to_lead(doc, cfg):
	"""The ONE fold: a resolved per-form submission row + its contract -> a routed CRM Lead via the
	partner brain. The single implementation the wildcard route_submission runs for every form.

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
	# No hardcoded phone field: mobile_no is populated by the mapping loop below from whichever
	# field the contract maps to lead.mobile_no (validated to exist on save). The brain's
	# _norm_phone + doc_events canonicalise the raw value to E.164.
	item = {}
	parent_fields = []
	child_allow = {}
	notes = []
	for m in cfg.mappings:
		val = _resolve_value(doc, m)
		if not val:
			continue
		# ONE target representation: the structured (target_table, target_field) pair,
		# routed by the section brain — never a private table/field dict.
		table = (m.target_table or "").strip()
		field = (m.target_field or "").strip()
		if not table:
			continue
		if table == "note":
			notes.append((field or "Note", val))
			continue
		if not frappe.db.exists("CRM Lead Section", table):
			continue  # stale/invalid target_table on an already-saved row — skip, don't throw
		section = frappe.get_cached_doc("CRM Lead Section", table)
		if section.child_table_field:
			item.setdefault(section.child_table_field, [{}])[0][field] = val
			child_allow.setdefault(section.child_table_field, []).append(field)
		else:
			item[field] = val
			parent_fields.append(field)

	# Provenance (latest-source-wins): stamp which intake form sourced this lead. Sent on
	# every upsert; the brain's doc.update(parent) applies it on update too — so the lead
	# always reflects its most recent source (see Phase 0 §provenance decision).
	# (custom_origin_vertical lives on CRM Lead and is set from the forced grain via routing —
	# never a form field; a former cfg.get("custom_origin_vertical") read here was dead and removed.)
	item["custom_source_origin"] = f"Intake form: {cfg.name}"
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
		).insert(ignore_permissions=True)  # authz-ok: tier-b — guest submit: routing is FORCED from the form, never the submitter

	_attach_files(doc, doc_lead.name)

	# Stamp the result back on the submission — but only on a doctype that carries these
	# result fields. A per-form runtime sink may not declare lead/processed, so guard each set
	# on field existence (no stamp != failed processing).
	meta = doc.meta
	if meta.has_field("lead"):
		doc.db_set("lead", doc_lead.name, update_modified=False)
	if meta.has_field("processed"):
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
	# cstr() so a checkbox/number/date value (non-string) never AttributeErrors on .strip();
	# the `or ""` keeps falsy values (unchecked box = 0, empty) skippable exactly as before.
	picked = frappe.cstr(doc.get(m.source_field) or "").strip()
	manual = frappe.cstr(doc.get(m.manual_field) or "").strip() if m.manual_field else ""

	# "manual wins" when nothing was picked, or the pick is an explicit Other sentinel
	if manual and (not picked or picked == "Others" or picked == "Other"):
		if m.master_doctype:
			# Look the master up / create it on the MASTER's OWN title_field (its display column) —
			# NOT the mapping's target_field, which is the CHILD-PROFILE column and can differ
			# (CRM Doctor.doctor_name vs the care profile's custom_doctor_name). The caller
			# (_fold_submission_to_lead) writes the returned canonical label to target_field.
			display_field = frappe.get_meta(m.master_doctype).get("title_field") or "name"
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

	# A.10: an untrusted anonymous (web-form Guest) submission never auto-creates a master — the
	# typed value is stored as text. Governed growth (review_pending) is for authenticated callers only.
	if frappe.session.user == "Guest":
		return canonical

	d = frappe.new_doc(doctype)
	d.set(display_field, canonical)
	# Governed growth (Phase 3): flag form-born rows for ops review/merge.
	if frappe.get_meta(doctype).has_field("review_pending"):
		d.set("review_pending", 1)
	d.insert(ignore_permissions=True)  # authz-ok: tier-b — guest submit: routing is FORCED from the form, never the submitter
	return d.get(display_field)


def _attach_files(doc, lead_name):
	"""Surface every uploaded attachment onto the lead so files show in its attachments.
	No hardcoded field name: we read the submission's OWN Attach / Attach Image fields, so a
	form can declare any attachment (prescription, report, ...) and all of them are linked."""
	from tatva_connect.storage import file_manager

	for df in doc.meta.fields:
		if df.fieldtype in ("Attach", "Attach Image"):
			url = doc.get(df.fieldname)
			if url:
				file_manager.link(
					url, attached_to_doctype="CRM Lead", attached_to_name=lead_name,
					meta={"custom_source": "Intake"},
				)
