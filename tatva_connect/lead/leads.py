"""CRM Lead automations."""
import frappe
from frappe import _
from frappe.utils import cstr

from tatva_connect.whatsapp.phone import to_e164

# Phone-type fields on CRM Lead we keep canonical (+E.164). mobile_no is the dedup +
# WhatsApp-inbound match key; the rest are normalized for consistency.
PHONE_FIELDS = ("mobile_no", "phone", "custom_alternate_number", "custom_caregiver_phone")

# Headline metrics surfaced on the core Lead for list/sort/kanban. Each maps a
# CRM Lead parent field -> the CRM Lab Profile child field it mirrors. Auto-synced
# from the LATEST lab row on every write; agents never hand-maintain these.
HEADLINE_LAB_MAP = {
	"custom_latest_hba1c": "hba1c",
	"custom_latest_fbs": "fbs",
	"custom_height_feet": "height_feet",
	"custom_weight_kg": "weight_kg",
	"custom_last_report_date": "report_date",
}

# Routing fields the dedup anchor keys on. An omitted field arrives as '' (form/import)
# or None (API); both MUST canonicalise to one value so the {mobile, vertical, group}
# anchor and every stored lead agree. We pick NULL: dedup_guard's get_value then builds
# IS NULL, which matches the NULL we store here (instead of missing '' rows).
ROUTING_FIELDS = ("custom_vertical", "custom_group", "custom_current_program")


def canonicalize_routing_fields(doc, method=None):
	"""Canonicalise empty routing fields ''->None on every write (before_validate),
	one direction, consistently. The dedup anchor keys on {mobile, vertical, group}; if
	some rows store '' and others NULL for an omitted line, get_value's IS NULL misses
	the '' rows -> a duplicate lead slips through. Storing only NULL closes that gap.
	Runs BEFORE dedup_guard (before_validate precedes validate)."""
	for f in ROUTING_FIELDS:
		if doc.get(f) == "":
			doc.set(f, None)


def normalize_lead_phones(doc, method=None):
	"""Canonicalise phone fields to +E.164 on every write (validate), so dedup and
	WhatsApp-inbound lookup are reliable no matter how a writer formatted the number.
	Runs BEFORE dedup_guard (hooks.py orders them)."""
	for f in PHONE_FIELDS:
		val = doc.get(f)
		if val:
			doc.set(f, to_e164(val))


def dedup_guard(doc, method=None):
	"""Block a duplicate CRM Lead on the same product line — per-line dedup.

	The dedup anchor is ``mobile_no + custom_vertical + custom_group``: one lead =
	one patient on one product-line+group. The SAME phone on a DIFFERENT line/group
	is a separate lead by design, so the guard only trips when all three match.
	Program is NOT part of identity — it is a mutable attribute (custom_current_program
	can transition on the one lead), so it never participates in dedup.

	Fires on EVERY insert path (REST POST, the CRM "Create Lead" modal, imports,
	the intake Web Form). The ``lead_create`` endpoint finds-or-updates an existing
	(phone, line, group) lead first, so a re-send never trips this guard.

	No mobile_no -> nothing to dedup (some leads are email/name only).
	"""
	if not doc.mobile_no:
		return

	mobile = to_e164(doc.mobile_no)  # compare on the canonical form (stored leads are canonical)
	existing = frappe.db.get_value(
		"CRM Lead",
		{
			"mobile_no": mobile,
			"custom_vertical": doc.custom_vertical,
			"custom_group": doc.custom_group,
			"name": ["!=", doc.name or ""],
		},
		"name",
	)
	if existing:
		frappe.throw(
			_(
				"A lead with mobile number {0} already exists on this product line ({1}). "
				"Update that lead instead."
			).format(mobile, existing),
			title=_("Duplicate lead"),
		)


def validate_stage(doc, method=None):
	"""Single combined Stage pick: custom_stage links one selectable leaf of
	CRM Lead Stage (a substage, or a main stage with no substages). Validate it's
	on the lead's program, and derive the read-only custom_main_stage for grouping.
	Empty = lifecycle not set yet. Soft, clear errors — no silent corrections.
	"""
	if not doc.custom_stage:
		doc.custom_main_stage = None
		return

	stage = frappe.db.get_value(
		"CRM Lead Stage", doc.custom_stage, ["program", "stage", "substage_of", "selectable"], as_dict=True
	)
	if not stage:
		return

	program = doc.custom_current_program
	if program and stage.program != program:
		frappe.throw(
			_("Stage {0} belongs to a different program. Pick a stage for {1}.").format(
				doc.custom_stage, program
			),
			title=_("Invalid stage"),
		)
	if not stage.selectable:
		frappe.throw(
			_("{0} is a grouping stage, not a pickable one. Pick a specific stage.").format(doc.custom_stage),
			title=_("Invalid stage"),
		)

	# derive the main stage: a substage's parent, else the stage itself
	if stage.substage_of:
		doc.custom_main_stage = frappe.db.get_value("CRM Lead Stage", stage.substage_of, "stage")
	else:
		doc.custom_main_stage = stage.stage


def _latest_lab_row(doc):
	"""The most recent CRM Lab Profile child row, or None. 'Latest' = newest
	report_date; rows with no date sort last, ties broken by grid order (idx)."""
	rows = doc.get("custom_lab_profile") or []
	if not rows:
		return None
	# cstr keys the sort uniformly whether report_date is a date object or an ISO
	# string (Frappe types child values inconsistently across write paths).
	return max(rows, key=lambda r: (cstr(r.report_date), r.idx or 0))


def sync_headline_metrics(doc, method=None):
	"""Copy the latest lab row's headline values up to the core Lead fields, so ops
	can sort/scan/kanban on them. Idempotent: re-derives from the child each write,
	whether the change came from the partner API or a UI/grid edit. No lab row leaves
	the headlines as-is (don't clobber on an unrelated save)."""
	# Defensive (P9): during the profile-restructure migration the lab table's columns
	# may briefly be out of sync with the doc meta — skip rather than throw on a live
	# Lead save if the source lab column is missing.
	if not frappe.db.has_column("CRM Lab Profile", "hba1c"):
		return
	row = _latest_lab_row(doc)
	if not row:
		return
	for parent_field, lab_field in HEADLINE_LAB_MAP.items():
		doc.set(parent_field, row.get(lab_field))


@frappe.whitelist()
def lead_section_gate(reference_name=None):
	"""Does this lead belong to the oncology drug-program world? The Data-tab gate
	(lead_data_tab_gate.js) calls this to decide which profile sections to show: a
	drug-program lead sees the Drug Program section and hides the metabolic ones; a
	metabolic lead does the reverse.

	The 'world' is CONFIG, not code: each `CRM Program` row self-declares it via the
	`custom_is_drug_program` flag. A new oncology program = a CRM Program row with that
	box ticked — no code change, and the JS never hardcodes a program list.

	Fail-safe: any error returns drug_program=False and is logged, so the gate shows
	everything (a wrong hide could strand data) rather than failing silently."""
	try:
		if not reference_name:
			return {"drug_program": False}
		program = frappe.db.get_value("CRM Lead", reference_name, "custom_current_program")
		is_drug = bool(program) and bool(frappe.db.get_value("CRM Program", program, "custom_is_drug_program"))
		return {"drug_program": is_drug}
	except Exception:
		frappe.log_error(title="lead_section_gate failed", message=frappe.get_traceback())
		return {"drug_program": False}
