# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Lead detail panel projection — the grain/brain-aware, LSQ-clean Data tab.

The Data tab used to render the raw doctype layout (clinical/program child tables as inline
grids) gated by a DOM-hacking CRM Form Script (lead/form_scripts/data_tab_gate.js). This module
replaces that with a clean, server-resolved projection driven by the EXISTING field catalog
(`CRM Lead API Field`) and the SAME grain-entitlement brain Smart Views uses — no new field
meanings, no DOM, no client field enumeration.

Two independent axes, kept separate on purpose:
  * ENTITLEMENT (viewer)  — may this principal see the field at all?
      access.entitlement.resolve_fields (grain brain + role restriction + universal floor).
  * APPLICABILITY (record) — does this section apply to THIS lead?
      the program "world" (CRM Program.custom_is_drug_program). A world is a SET of programs,
      which a single grain axis cannot express, so applicability uses the config flag — this
      relocates the retired lead_section_gate decision server-side, cleanly.

Security:
  * Read is permission-gated; values are resolved server-side (no client SQL, no field
    enumeration) — there is NO string-built SQL in this module; everything flows through the
    frappe doc API.
  * Write goes through a SERVER-BUILT allowlist (entitled ∧ applicable ∧ not read-only ∧ not a
    protected routing field). A field_key outside it is rejected — a crafted payload can never
    reach routing/owner/out-of-grain/read-only fields (no mass-assignment).
"""
import frappe
from frappe import _

from tatva_connect.access import entitlement

# section_key -> (display label, sort order, world). world ∈ {"neutral","drug","metabolic"}.
# Mirrors the retired lead_section_gate split so existing behaviour is preserved, delivered
# cleanly. `drug` and `drug_program` share one display group ("Drug Program").
SECTION_REGISTRY = {
	"lead":         ("Lead Details",     10, "neutral"),
	"acq":          ("Acquisition",      20, "neutral"),
	"plan":         ("Plan",             30, "metabolic"),
	"lab":          ("Lab",              40, "metabolic"),
	"clinical":     ("Health Snapshot",  50, "metabolic"),
	"care":         ("Care & Providers", 60, "metabolic"),
	"drug":         ("Drug Program",     70, "drug"),
	"drug_program": ("Drug Program",     70, "drug"),
}
_DEFAULT_SECTION = ("Lead Details", 10, "neutral")

# Fields that live on CRM Task (activity surfaces) — never part of the lead profile.
_ACTIVITY_DOCTYPES = ("CRM Task", "CRM Task Order Detail")

# Identity/routing fields: shown (informative) but NEVER editable on this panel. Identity
# (vertical/group) is the dedup anchor; program transitions happen via deliberate routing
# flows, not a casual field edit. Forced read-only regardless of catalog/property-setter state.
_PROTECTED_FIELDS = frozenset({"custom_vertical", "custom_group", "custom_current_program"})

_CATALOG_FIELDS = [
	"field_key", "label", "fieldname", "section_key", "target_doctype",
	"child_table_field", "child_pick", "applies_to",
	"grain_vertical", "grain_group", "grain_program",
]


# ------------------------------- pure helpers (no DB) -------------------------------
def _section_meta(section_key):
	return SECTION_REGISTRY.get(section_key or "", _DEFAULT_SECTION)


def section_applies(section_key, is_drug):
	"""Does this section apply to a lead in (drug) / out of (metabolic) the drug world? Pure.
	A neutral section always applies; an unknown section_key buckets to neutral (never hidden)."""
	world = _section_meta(section_key)[2]
	if world == "drug":
		return bool(is_drug)
	if world == "metabolic":
		return not is_drug
	return True


def is_profile_row(row):
	"""A lead-profile catalog row (vs an activity-surface / Smart-Views-only row). Pure.
	Excludes rows scoped to an activity (applies_to 'activity:*') and rows on CRM Task."""
	if (row.get("applies_to") or "").strip().startswith("activity:"):
		return False
	if (row.get("target_doctype") or "") in _ACTIVITY_DOCTYPES:
		return False
	return True


def dedup_rows(rows):
	"""Collapse duplicate catalog rows to one per (target_doctype, fieldname). Pure.
	The curated lead-surface row (applies_to == 'lead') wins over the raw partner row, so the
	cleaner label + flatten rule is used. Order preserved by first appearance."""
	chosen, order = {}, []
	for row in rows:
		key = (row.get("target_doctype") or "", row.get("fieldname") or "")
		if key not in chosen:
			chosen[key], _ = row, order.append(key)
			continue
		if (row.get("applies_to") or "") == "lead" and (chosen[key].get("applies_to") or "") != "lead":
			chosen[key] = row
	return [chosen[k] for k in order]


def writable_keys(selected, is_readonly):
	"""The field_keys writable through update_lead_detail: those in `selected` whose target field
	is not read-only. `is_readonly(target_doctype, fieldname) -> bool` is injected (pure/testable).
	`selected` is {field_key: row}."""
	return {fk for fk, row in selected.items()
	        if not is_readonly(row.get("target_doctype") or "", row.get("fieldname") or "")}


def _is_empty(value):
	if value is None:
		return True
	if isinstance(value, str):
		return value.strip() == ""
	if isinstance(value, (list, tuple, dict)):
		return len(value) == 0
	return False  # 0 / False are real values, never "empty"


# ------------------------------- frappe-bound core -------------------------------
def _catalog_rows():
	"""Every catalog row, keyed by field_key. Read here (not via smartview) so this module has
	no coupling to the Smart Views composer."""
	rows = frappe.get_all("CRM Lead API Field", fields=_CATALOG_FIELDS, order_by="field_key asc")
	return {r["field_key"]: r for r in rows}


def _is_drug_lead(program):
	return bool(program) and bool(frappe.db.get_value("CRM Program", program, "custom_is_drug_program"))


def _docfield(target_doctype, fieldname):
	try:
		return frappe.get_meta(target_doctype).get_field(fieldname)
	except Exception:
		return None


def _is_readonly(target_doctype, fieldname):
	"""Read-only iff a protected routing field, or the docfield is read_only, or unknown
	(fail-closed: an unresolvable field is never writable)."""
	if fieldname in _PROTECTED_FIELDS:
		return True
	df = _docfield(target_doctype, fieldname)
	return bool(df.read_only) if df else True


def _select(doc):
	"""The entitled (viewer) ∧ applicable (this lead's world) ∧ deduped {field_key: row}.
	AXIS 1 entitlement → entitlement.resolve_fields; AXIS 2 applicability → section_applies."""
	catalog = {k: r for k, r in _catalog_rows().items() if is_profile_row(r)}
	visible = entitlement.resolve_fields(catalog, entitlement.entitled_grains(), frappe.get_roles())
	is_drug = _is_drug_lead(doc.get("custom_current_program"))
	applicable = {k: r for k, r in visible.items()
	              if section_applies(r.get("section_key"), is_drug) or k in entitlement.UNIVERSAL_KEYS}
	deduped = dedup_rows(list(applicable.values()))
	return {r["field_key"]: r for r in deduped}


def _child_row(doc, row):
	"""The single child row a child-section field reads from, honouring child_pick
	('single' | 'latest_by:<field>'; default 'single'). Returns a child doc or None."""
	table = row.get("child_table_field")
	if not table:
		return None
	children = doc.get(table) or []
	if not children:
		return None
	pick = (row.get("child_pick") or "single").strip()
	if pick.startswith("latest_by:"):
		order_field = pick.split(":", 1)[1].strip()
		if order_field:
			return max(children, key=lambda c: (c.get(order_field) or ""))
	return children[0]


def _value(doc, row):
	if row.get("child_table_field"):
		child = _child_row(doc, row)
		return None if child is None else child.get(row.get("fieldname"))
	return doc.get(row.get("fieldname"))


def _group_key(section_key):
	if section_key in ("drug", "drug_program"):
		return "drug"
	return section_key if section_key in SECTION_REGISTRY else "lead"


@frappe.whitelist()
def lead_detail(lead):
	"""Read projection: {sections:[{key,label,order,fields:[{field_key,label,fieldname,fieldtype,
	options,value,empty,read_only}]}]}. Permission-gated; values resolved server-side."""
	frappe.has_permission("CRM Lead", "read", doc=lead, throw=True)
	doc = frappe.get_doc("CRM Lead", lead)
	buckets = {}
	for fk, row in _select(doc).items():
		sk = row.get("section_key") or ""
		label, order, _ = _section_meta(sk)
		gk = _group_key(sk)
		bucket = buckets.setdefault(gk, {"key": gk, "label": label, "order": order, "fields": []})
		df = _docfield(row.get("target_doctype"), row.get("fieldname"))
		value = _value(doc, row)
		bucket["fields"].append({
			"field_key": fk,
			"label": row.get("label") or row.get("fieldname"),
			"fieldname": row.get("fieldname"),
			"fieldtype": df.fieldtype if df else "Data",
			"options": (df.options or "") if df else "",
			"value": value,
			"empty": _is_empty(value),
			"read_only": _is_readonly(row.get("target_doctype"), row.get("fieldname")),
			# order = the field's position in its target doctype (operator-controlled, not hardcoded)
			"_idx": df.idx if df else 10_000,
		})
	for b in buckets.values():
		b["fields"].sort(key=lambda f: (f.pop("_idx"), f["label"]))
	sections = sorted(buckets.values(), key=lambda s: s["order"])
	return {"sections": sections}


def _stage_write(doc, row, value):
	"""Stage one field write onto the in-memory doc (parent field or single child row). Goes
	through the doc API only — never raw SQL."""
	table = row.get("child_table_field")
	fieldname = row.get("fieldname")
	if not table:
		doc.set(fieldname, value)
		return
	child = _child_row(doc, row) or doc.append(table, {})
	child.set(fieldname, value)


@frappe.whitelist()
def update_lead_detail(lead, changes):
	"""Write path. `changes` is {field_key: value}. Only field_keys in the SERVER-BUILT writable
	projection (entitled ∧ applicable ∧ not read-only ∧ not protected) are accepted; anything else
	is rejected. Persists via doc.save() so field perms, validate and doc_events all re-fire."""
	frappe.has_permission("CRM Lead", "write", doc=lead, throw=True)
	changes = frappe.parse_json(changes) if isinstance(changes, str) else (changes or {})
	if not isinstance(changes, dict):
		frappe.throw(_("Invalid changes payload"))
	doc = frappe.get_doc("CRM Lead", lead)
	selected = _select(doc)
	writable = writable_keys(selected, _is_readonly)
	for fk, value in changes.items():
		if fk not in writable:
			frappe.throw(_("Field {0} is not editable here").format(fk))
		_stage_write(doc, selected[fk], value)
	doc.save()
	return {"ok": True}
