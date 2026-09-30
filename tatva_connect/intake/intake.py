"""Web intake: one public form, one submission table, one record saved from it.

Each `CRM Intake Form` gets its OWN submission DocType and Web Form (the builder). A single wildcard
after_insert (`route_submission`) checks the answers every form's answers pass (`check_answers`), then
saves the record the form creates: a routed, deduped CRM Lead through the lead's own brain, or a layer
target (`layers`) such as an HD Ticket. Adding a form needs only a new `CRM Intake Form` row.
"""
import frappe
from frappe import _

from tatva_connect import automation
from tatva_connect.intake import layers
from tatva_connect.propagate import fail_safe
from tatva_connect.whatsapp.phone import is_mobile, to_e164

# The one column every submission table carries, independent of its questions: the hidden back-link to its intake form.
INTAKE_FORM_FIELD = "intake_form"
# A pick that means "not listed": it opens the question's typed companion box.
OTHER_SENTINELS = ("Others", "Other")


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


def _intake_doctypes() -> dict:
	"""Submission sink DocType -> the CRM Intake Form it belongs to, for every ENABLED form.

	The cheap guard the wildcard router checks (`doc.doctype not in _intake_doctypes()`), and —
	because it is a map, not a set — the ONE way back from a sink to its contract. The throttle
	needs that reverse hop to find which question carries the phone; a second walk of the same
	rows to answer it would be a second brain. Memoised; busted on any CRM Intake Form write
	(and on every sync_form). Names are DERIVED via the builder, so there is no column to read."""
	cached = frappe.cache().get_value(_INTAKE_DOCTYPES_CACHE_KEY)
	if cached is None:
		from tatva_connect.intake.builder import safe_doctype_name_for

		cached = {}
		unroutable = []
		# The wildcard fires site-wide, incl. during install before the contract table exists.
		if frappe.db.table_exists("CRM Intake Form"):
			for cfg in frappe.get_all("CRM Intake Form", filters={"enabled": 1}, fields=["name", "form_name"]):
				dt = safe_doctype_name_for(frappe._dict(cfg))  # None for a legacy/invalid name
				if dt and frappe.db.exists("DocType", dt):
					cached[dt] = cfg.name
				else:
					unroutable.append((cfg.name, cfg.form_name, dt))
		# Cache BEFORE logging, never inside the loop: log_error INSERTS an Error Log, which fires the
		# same wildcard after_insert that calls this function — a cold cache there would rebuild, log,
		# and recurse without end. With the cache already set, the re-entrant call returns immediately.
		frappe.cache().set_value(_INTAKE_DOCTYPES_CACHE_KEY, cached)
		for name, form_name, dt in unroutable:
			# An ENABLED form with no sink routes nothing — save it to scaffold, or disable it.
			frappe.log_error(
				title="Intake form is enabled but routes nothing",
				message=f"form={name} form_name={form_name} derived_doctype={dt}",
			)
	return cached


def bust_intake_doctype_cache(doc=None, method=None):
	"""Drop the memoised intake-doctype guard set. doc_events hook on CRM Intake Form
	(on_update/on_trash) AND called by builder.sync_form, so the wildcard router's guard
	never serves a stale set after a form is added, toggled, or scaffolded."""
	frappe.cache().delete_value(_INTAKE_DOCTYPES_CACHE_KEY)


def route_submission(doc, method=None):
	"""Wildcard after_insert (doc_events["*"]) — the ONE entry for every intake submission. Fires site-wide,
	so it returns at once for any doctype that is not an enabled form's submission table (one cached
	membership test); runtime submission tables can carry no hooks of their own. On a hit it saves the record."""
	if doc.doctype not in _intake_doctypes():
		return
	if not automation.is_enabled("Lead::Enrolment::intake"):
		return
	if not doc.get(INTAKE_FORM_FIELD):
		return
	# The savepoint is taken in _route_one, never here: this guard runs on EVERY insert site-wide, and a mark+release pair on each one would charge the whole site two round trips per save.
	_route_one(doc, method)


# PROPAGATE (@fail_safe): the submission row IS the patient's answers — every mapped question is a column on it — so a fold lost to a deadlock or a duplicate key is rebuildable from the row, and `processed` stays 0 to say so. Unwrapped, the fold's exception rolled the row back too and there was nothing left to rebuild from.
@fail_safe
def _route_one(doc, method=None):
	"""The save, isolated behind the house savepoint: an accident is undone and logged, leaving the
	submission row as the record of what was sent. A refusal (`frappe.throw`) still surfaces — the person
	is on the page and can correct it, and a thank-you for a record that was never saved would be worse."""
	cfg = frappe.get_cached_doc("CRM Intake Form", doc.get(INTAKE_FORM_FIELD))
	if not cfg.enabled:
		return
	# Before the mute, so the visitor reads why an answer was refused.
	check_answers(doc, cfg)
	frappe.flags.mute_messages = True  # public form: never surface internal notices to the visitor; request-scoped
	if layers.layer_of(cfg):
		layers.fold(doc, cfg)
	else:
		_fold_submission_to_lead(doc, cfg)


def resolve_link_filters(rules, values):
	"""A question's link_filters as frappe filters, each `eval:doc.<question>` read from `values`; a rule whose question is unanswered is not applied yet."""
	resolved = []
	for doctype, field, op, value in rules:
		if layers.eval_ref(value):
			value = values.get(layers.eval_ref(value))
			if not value:
				continue
			if op == "=":
				# A row that names no parent is ungated and passes every parent — the rule the picklist cascade reads with.
				op, value = "in", [value, ""]
		resolved.append([doctype, field, op, value])
	return resolved


def allowed_link_value(df, value, values):
	"""Is `value` a pick the question's own link_filters allow — the same question the page asks through `api.link_options`?"""
	if not df.link_filters:
		return True
	filters = resolve_link_filters(frappe.parse_json(df.link_filters), values)
	return bool(frappe.get_all(df.options, filters=[*filters, [df.options, "name", "=", value]], limit=1))


def check_answers(doc, cfg):
	"""The checks every intake form's answers pass before any record is built, whatever that record is."""
	answers = doc.as_dict()
	for df in doc.meta.fields:
		value = doc.get(df.fieldname)
		if df.fieldtype == "Link" and value and not allowed_link_value(df, value, answers):
			frappe.throw(_("{0} is not a valid choice for {1}.").format(frappe.bold(value), frappe.bold(_(df.label))))
	for m in cfg.mappings:
		value = doc.get(m.source_field)
		if not (m.get("mobile_only") and value):
			continue
		to_e164(value, fieldname=m.label or m.source_field)  # a malformed number is refused in the store gate's own words
		if not is_mobile(value):
			frappe.throw(_("{0} is not a mobile number.").format(frappe.bold(value)), title=_("Invalid Phone Number"))


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
		table, field = layers.target_pair(m)
		if not table:
			continue
		if table == "note":
			notes.append((field or "Note", val))
			continue
		if not frappe.db.exists("CRM Lead Section", table):
			# Stale/invalid target_table on an already-saved row — skip, don't throw, but never quietly.
			frappe.log_error(
				title="Intake answer dropped: unknown target section",
				message=f"form={cfg.name} submission={doc.doctype}/{doc.name} question={m.source_field} target_table={table}",
			)
			continue
		# No catalogue check here: the fold must never silently DROP a patient's answer — the target is gated at the picker and the save-time backstop, where an operator can act on it.
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

	attach_files(doc, layers.LEAD, doc_lead.name)
	stamp(doc, layers.result_field(cfg), doc_lead.name)


def stamp(doc, result_field, name):
	"""Stamp the record a fold built back on its submission — only on a sink that carries the column (no stamp != failed processing)."""
	if doc.meta.has_field(result_field):
		doc.db_set(result_field, name, update_modified=False)
	if doc.meta.has_field("processed"):
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
	# Resolved to the HUMAN label first: a Link's value is its composite PK, so testing the raw value against the Other sentinel below never matched and the typed companion was ignored.
	raw = frappe.cstr(doc.get(m.source_field) or "").strip()
	picked = _link_label(doc, m.source_field, raw) if raw else ""
	manual = frappe.cstr(doc.get(m.manual_field) or "").strip() if m.manual_field else ""

	# "manual wins" when nothing was picked, or the pick is an explicit Other sentinel
	if manual and (not picked or picked in OTHER_SENTINELS):
		if m.master_doctype:
			# Look the master up / create it on the MASTER's OWN title_field (its display column) —
			# NOT the mapping's target_field, which is the CHILD-PROFILE column and can differ
			# (CRM Doctor.doctor_name vs the care profile's custom_doctor_name). The caller
			# (_fold_submission_to_lead) writes the returned canonical label to target_field.
			display_field = frappe.get_meta(m.master_doctype).get("title_field") or "name"
			canonical = _ensure_master(m.master_doctype, display_field, manual)
			return canonical or manual
		return manual
	return picked  # already the human label, never the opaque composite key


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


def attach_files(doc, doctype, name):
	"""Surface every uploaded attachment onto the record the fold built so files show in its attachments.
	No hardcoded field name: we read the submission's OWN Attach / Attach Image fields, so a
	form can declare any attachment (prescription, report, ...) and all of them are linked.

	Each file is isolated behind its OWN savepoint: linking runs after the lead is already built,
	so a failure on one attachment must never roll the lead — or the other attachments — back with
	it. The File row still exists and is still bonded to the submission, so a missed link is
	recoverable; a lost lead is not. Every failure is logged."""
	from tatva_connect.storage import file_manager

	for df in doc.meta.fields:
		if df.fieldtype not in ("Attach", "Attach Image"):
			continue
		url = doc.get(df.fieldname)
		if not url:
			continue
		sp = f"intake_attach_{frappe.generate_hash(length=8)}"
		frappe.db.savepoint(sp)
		try:
			file_manager.link(
				url, attached_to_doctype=doctype, attached_to_name=name,
				meta={"custom_source": "Intake"},
			)
		except Exception:
			frappe.db.rollback(save_point=sp)
			frappe.log_error(
				title="Intake attachment link failed",
				message=f"{doctype}={name} field={df.fieldname}\n\n{frappe.get_traceback()}",
			)
