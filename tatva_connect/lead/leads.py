"""CRM Lead automations."""
import frappe
from frappe import _

from tatva_connect import automation
from tatva_connect.lead import multirow
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
	if not automation.is_enabled("Lead::CRM Lead::dedup"):
		return
	for f in ROUTING_FIELDS:
		if doc.get(f) == "":
			doc.set(f, None)


def stamp_entitled_grain(doc, method=None):
	"""Stamp / clamp the lead's grain from the acting user's entitlement — the ONE grain brain
	(access.entitlement). A user NEVER free-picks grain: a single-grain user's grain is auto-applied
	(no picker); a manager's picked grain (sent by the form) is clamped to entitlement; a System
	Manager may set any. Fail-closed for interactive users — no entitlement, an unentitled grain, or a
	blank grain with multiple options is rejected.

	Trusted server paths (partner API, intake) insert with ignore_permissions and force their own
	grain via _force_routing, so they already carry the clamp (same source, access.entitlement) —
	skip them here. Runs before canonicalize_routing_fields so dedup sees the stamped grain."""
	if doc.flags.ignore_permissions:
		return
	if not automation.is_enabled("Lead::CRM Lead::grain"):
		return
	from tatva_connect.access.entitlement import ALL_GRAINS, REGISTRY_FLAG, entitled_grains, grain_entitled
	from tatva_connect.api._base import throw_field

	grains = entitled_grains()
	if grains == ALL_GRAINS:
		return  # System Manager — trusts the form's pick (any grain).
	if not grains:
		frappe.throw(
			_("This account is entitled to no grain, so it cannot create a lead. Ask the operator to "
			  "grant an entitlement for the product line and group the lead belongs to."),
			frappe.PermissionError,
		)

	# Entitlement is a REGION; a lead is a POINT. A region that wildcards an axis has to be resolved to
	# one leaf before the lead can be filed. Flag OFF → skipped entirely, so the path below is today's.
	if len(grains) == 1 and automation.is_enabled(REGISTRY_FLAG):
		_resolve_wildcard_axes(doc, next(iter(grains)))

	if not any(doc.get(f) for f in ROUTING_FIELDS):
		# Single grain → apply silently, no question. Multiple (manager) → the form must send the pick.
		if len(grains) == 1:
			vertical, group, program = next(iter(grains))
			doc.custom_vertical = vertical or None
			doc.custom_group = group or None
			doc.custom_current_program = program or None
			return
		# One message serves both readers here: the Desk wording named no field and offered no values
		# either, so naming the three fields and listing the entitled grains is the fix for BOTH.
		throw_field(
			_("This lead carries no grain, and more than one is available. Set custom_vertical, "
			  "custom_group and custom_current_program to one of: {0}.").format(_grain_options(grains)),
			list(ROUTING_FIELDS),
		)

	grain = (doc.custom_vertical or "", doc.custom_group or "", doc.custom_current_program or "")
	if not grain_entitled(grain):
		throw_field(
			_("The grain {0} is not one this account may write to. Set custom_vertical, custom_group "
			  "and custom_current_program to one of: {1}.").format(
				_grain_options([grain]), _grain_options(grains)),
			list(ROUTING_FIELDS), frappe.PermissionError,
		)


def _resolve_axis(supplied, options, fieldname, label):
	"""One wildcard axis: the form's pick if it sent one, blank if the registry declares no children
	under this node, otherwise a refusal that names the values which WOULD be accepted.

	The pick is not trusted here — `grain_entitled` clamps it afterwards (region-covers AND a real
	CRM Grain row), so an off-tree value is refused by the same brain that answers every other grain
	question rather than by a second check written here."""
	from tatva_connect.api._base import throw_field

	if supplied:
		return supplied
	if not options:
		return None  # nothing declared under this node — a genuinely programme-less group stays blank
	throw_field(
		_("This lead needs a {0}. This account covers the whole group, so each lead must be filed under "
		  "exactly one of: {1}.").format(label, ", ".join(options)),
		[fieldname],
	)


def _resolve_wildcard_axes(doc, region):
	"""Fill the lead's grain from the acting user's REGION, requiring a pick where the region wildcards
	an axis that has declared children. Flag-gated; the caller checks `Access::Grain::registry`.

	The region's CONCRETE axes are applied silently — they were never a choice. A WILDCARD axis is taken
	from the form's pick, and if nothing was picked while the registry declares children, the save is
	refused instead of filing the lead under "any" — which is exactly how blank-programme leads were
	produced before, and why a rep covering all of Anaya could not create a Nivolumab lead.

	Cascading, because a programme is only meaningful under a settled group: the group is resolved first,
	and the programme's options are read under the group that resolution produced. A wildcard VERTICAL is
	left to the existing path — the registry exposes no verticals helper, and no entitlement in this
	deployment wildcards it; inventing one here would be a second source for the same question."""
	from tatva_connect.access.entitlement import groups_under, programs_under

	vertical, group, program = region
	if not vertical:
		return
	doc.custom_vertical = vertical
	if not group:
		group = _resolve_axis(doc.custom_group, groups_under(vertical), "custom_group", _("Group"))
	doc.custom_group = group or None
	if group and not program:
		program = _resolve_axis(
			doc.custom_current_program, programs_under(vertical, group),
			"custom_current_program", _("Program"),
		)
	doc.custom_current_program = program or None


def _grain_options(grains):
	"""Grain tuples rendered for a refusal — a blank axis reads `any`, the same wildcard meaning
	`taxonomy/grain.py` gives it, so the values offered are the values that will actually match."""
	return "; ".join(" / ".join(axis or "any" for axis in g) for g in sorted(grains))


def normalize_lead_phones(doc, method=None):
	"""Canonicalise phone fields to +E.164 on every write (validate), so dedup and
	WhatsApp-inbound lookup are reliable no matter how a writer formatted the number.
	Runs BEFORE dedup_guard (hooks.py orders them)."""
	if not automation.is_enabled("Lead::CRM Lead::dedup"):
		return
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
	if not automation.is_enabled("Lead::CRM Lead::dedup"):
		return
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
		from tatva_connect.api._base import throw_field

		throw_field(
			_("A lead with mobile number {0} already exists on this product line ({1}). "
			  "Update that lead instead.").format(mobile, existing),
			["mobile_no"], title=_("Duplicate lead"),
		)


def validate_stage(doc, method=None):
	"""Single combined Stage pick: custom_substage links one selectable leaf of
	CRM Lead Stage (a substage, or a main stage with no substages). Validate it's
	on the lead's program, and derive the read-only parent custom_stage for grouping.
	Empty = lifecycle not set yet. Soft, clear errors — no silent corrections.
	"""
	if not automation.is_enabled("Lead::CRM Lead::stage"):
		return
	if not doc.custom_substage:
		doc.custom_stage = None
		return

	stage = frappe.db.get_value(
		"CRM Lead Stage", doc.custom_substage, ["program", "stage", "substage_of", "selectable"], as_dict=True
	)
	if not stage:
		return

	from tatva_connect.api._base import throw_field

	program = doc.custom_current_program
	if program and stage.program != program:
		throw_field(
			_("Stage {0} belongs to the {1} programme and this lead is on {2}. Set custom_substage to a "
			  "stage of {2}.").format(doc.custom_substage, stage.program, program),
			["custom_substage"], title=_("Stage not on this programme"),
		)
	if not stage.selectable:
		throw_field(
			_("{0} groups other stages and is not one a lead sits at. Set custom_substage to one of the "
			  "stages it groups.").format(doc.custom_substage),
			["custom_substage"], title=_("Stage not selectable"),
		)

	# custom_stage is a Link -> CRM Lead Stage, so it must hold a stage NAME (PK `program::stage`),
	# never a bare label. Two-level grain (the leaf has a parent) -> the parent's PK; flat grain
	# (no parent, e.g. Anaya/TatvaPractice) -> the leaf itself IS the stage, so mirror the sub-stage.
	doc.custom_stage = stage.substage_of or doc.custom_substage


@frappe.whitelist()
def lead_stages(lead):
	"""Stages available to this lead's grain — the header-pill + picker source.
	Mirror of activity.api.list_types_for_lead: ONE indexed query, selectable
	leaves only, ordered by position. A blank `program` on a stage = wildcard
	(forward-compat; today every stage is program-scoped). Server-scoped: the
	program comes from the lead, never the client (invariant 9)."""
	frappe.has_permission("CRM Lead", "read", doc=lead, throw=True)
	program = frappe.db.get_value("CRM Lead", lead, "custom_current_program") or ""
	# Blank program = wildcard: ["in", ["", None, program]] reproduces (= '' OR IS NULL OR = program).
	# Equivalence vs the old raw SQL verified on dev across all 7 program values (0 NULLs, identical rows).
	return frappe.get_all(
		"CRM Lead Stage",
		filters={"selectable": 1, "program": ["in", ["", None, program]]},
		fields=["name", "stage", "substage_of", "display_label", "color", "position"],
		order_by="position asc, stage asc",
	)


def _latest_lab_row(doc):
	"""The most recent CRM Lab Profile child row, or None — via the ONE multi-row rule
	(multirow.latest_child_row), keyed by the 'lab' section's own row_key_field, NOT a hardcoded
	report_date. So the headline sync agrees with the Data tab and Smart Views on which row is 'latest'."""
	section = frappe.get_cached_doc("CRM Lead Section", "lab")
	rows = doc.get(section.child_table_field) or []
	return multirow.latest_child_row(rows, section.row_key_field)


def sync_headline_metrics(doc, method=None):
	"""Copy the latest lab row's headline values up to the core Lead fields, so ops
	can sort/scan/kanban on them. Idempotent: re-derives from the child each write,
	whether the change came from the partner API or a UI/grid edit. No lab row leaves
	the headlines as-is (don't clobber on an unrelated save)."""
	if not automation.is_enabled("Lead::CRM Lead::headline"):
		return
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


# lead_section_gate() RETIRED — the Data tab is now a clean server-side projection
# (tatva_connect.lead.detail) rendered by the native fork panel (tatva/DetailPanel.vue). The
# drug-vs-metabolic "world" split was itself retired too: the field CATALOG (CRM Lead API Field)
# is the SOLE authority — every catalogued field for the grain surfaces, empties hidden in the UI.
# The old DOM caller (lead/form_scripts/data_tab_gate.js) is archived to
# archive/lead-detail-native-promotion/. No DOM section-hiding hack remains.
