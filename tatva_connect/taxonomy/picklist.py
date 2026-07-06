"""Server-scoped Link query for the Picklist Engine (authenticated, read-only).

The form wires a multi-select / Link field at CRM Picklist Value through this custom query:

    field.get_query = () => ({
        query: 'tatva_connect.taxonomy.picklist.picklist_query',
        filters: { category, lead }            // OR explicit { category, vertical, group, program }
    })

GRAIN IS DERIVED SERVER-SIDE — this is the security boundary. Unlike taxonomy/lookups.py
(allow_guest, trusts the client's grain — the PUBLIC web-form pattern, forbidden here), this
method is @frappe.whitelist() (NEVER allow_guest) and NEVER trusts a client-supplied grain:

  * If a `lead` is passed, the grain is RE-READ from that lead's own
    custom_vertical / custom_group / custom_current_program (and the read itself is permission
    checked, so a caller can't borrow a lead they can't see).
  * If explicit axes are passed, EACH is CLAMPED through entitlement.grain_entitled() and
    frappe.throw(PermissionError) on a miss — mirroring smartview._grains_from_axes.

Options returned: rows of the requested `category` whose grain matches (matching axes OR a
blank/global axis), optionally gated by a cascading depends_on_field/value (the parent value
itself validated within the entitled grain). 2+ chars, capped. Returns (name, display_label)
so the Link stores the composite PK but shows the human label.
"""
import frappe
from frappe import _
from frappe.utils import cint

from tatva_connect.access import entitlement

_CAP = 50
_MIN = 2

# CRM Picklist Value grain columns, in (vertical, group, program) order — the entitlement tuple shape.
_AXES = ("vertical", "group", "program")
# The lead's own grain axes (re-read server-side; never trust a client grain).
_LEAD_AXES = ("custom_vertical", "custom_group", "custom_current_program")


def _filters(filters):
	"""Link queries may hand `filters` as a dict or a JSON string — normalize to a dict."""
	if isinstance(filters, str):
		filters = frappe.parse_json(filters)
	return frappe._dict(filters or {})


def _grain_from_lead(lead):
	"""The (vertical, group, program) grain re-read from the lead itself — never the client's.
	get_doc enforces the caller's read permission, so a lead the caller can't see throws here.
	Read permission is necessary but NOT sufficient: a lead may be shared/assigned outside the
	caller's entitled_grains, so the lead's grain is CLAMPED through entitlement (same brain as
	_grain_from_axes / the Smart View clamp) — else cross-grain options could be enumerated."""
	doc = frappe.get_doc("CRM Lead", lead)
	doc.check_permission("read")
	grain = tuple((doc.get(f) or "") for f in _LEAD_AXES)
	if not entitlement.grain_entitled(grain):
		frappe.throw(_("You are not entitled to this grain."), frappe.PermissionError)
	return grain


def _grain_from_axes(f):
	"""An explicit grain from request axes, CLAMPED to entitlement (mirrors
	smartview._grains_from_axes): a grain the caller isn't entitled to is rejected fail-closed."""
	grain = ((f.vertical or "").strip(), (f.group or "").strip(), (f.program or "").strip())
	if not entitlement.grain_entitled(grain):
		frappe.throw(_("You are not entitled to this grain."), frappe.PermissionError)
	return grain


def _resolve_grain(f):
	"""The grain this query scopes to, derived SERVER-SIDE. Prefer the lead's own stored grain
	(re-read + permission-checked); else the explicit axes, clamped through entitlement."""
	if f.lead:
		return _grain_from_lead(f.lead)
	return _grain_from_axes(f)


def _grain_filters(grain):
	"""Frappe filters matching this grain: each axis equals the grain value OR is blank/global.
	(blank-axis rows are wildcards visible in every grain.)

	No `IS NULL` branch is needed because the three axes are `not_nullable` NOT-NULL DEFAULT ''
	columns (crm_picklist_value.json): even an operator `INSERT IGNORE` that omits a column gets
	'' from the DB default, never NULL. So `= val OR = ''` here and the PQC backstop
	(access.picklist._grain_clause) clamp the exact same rows — the two paths can never diverge."""
	out = {}
	for col, val in zip(_AXES, grain, strict=False):
		out[col] = ["in", [val, ""]] if val else ""
	return out


@frappe.whitelist()
def picklist_query(doctype, txt, searchfield, start, page_len, filters):
	"""Grain-scoped picklist options for a Link/Table-MultiSelect field.

	filters (one of):
	  {category, lead}                      -> grain re-derived from the lead (preferred), OR
	  {category, vertical, group, program}  -> explicit axes, clamped to entitlement.
	  + optional {depends_on_field, depends_on_value} -> only options that either declare no
	    dependency, or whose declared (field, value) matches the form's current value.

	Returns [(name, display_label), ...] — txt needs 2+ chars, capped at _CAP.
	"""
	f = _filters(filters)
	category = (f.category or "").strip()
	if not category:
		return []
	txt = (txt or "").strip()
	if len(txt) < _MIN:
		return []

	grain = _resolve_grain(f)  # server-side; throws on an out-of-entitlement grain or unseeable lead

	conds = {"category": category, "display_label": ["like", f"%{txt}%"]}
	conds.update(_grain_filters(grain))

	# Cascading picklist: when the form supplies the parent field's current value, keep only
	# options that match it OR declare no dependency. The parent value is just a value test on
	# rows already clamped to the entitled grain, so it can't widen scope.
	dep_field = (f.depends_on_field or "").strip()
	dep_value = (f.depends_on_value or "").strip()
	if dep_field:
		conds["depends_on_field"] = ["in", [dep_field, ""]]
		conds["depends_on_value"] = ["in", [dep_value, ""]]

	return frappe.get_all(  # authz-ok: grain-clamped by _resolve_grain (entitlement + lead check_permission)
		"CRM Picklist Value",
		filters=conds,
		fields=["name", "display_label"],
		order_by="position asc, display_label asc",
		limit=min(cint(page_len) or 20, _CAP),
		as_list=True,
	)


# --------------------------------------------------------------------------------------------
# Ingestion + discovery seam (used by the partner API lead_schema AND the CRM Lead before_validate
# resolver). BOTH ride the SAME master + the SAME grain rule (_grain_filters) as picklist_query
# above — so the values the API ADVERTISES are exactly the values the resolver ACCEPTS: discovery
# and ingestion can never drift. Grain is (vertical, group, program); an axis matches the grain
# value OR is blank/global. These take an already-resolved grain tuple (no entitlement clamp): the
# callers derive grain from the lead's own stamped routing / the partner's forced mapping, so the
# grain is trusted server-state, not a client input.
# --------------------------------------------------------------------------------------------
def category_of(fieldname):
	"""The picklist `category` a field reads from: the fieldname minus a leading `custom_`
	(the convention the seeds + migration.resolve._category use). e.g. custom_nivo_indication
	-> nivo_indication."""
	fn = fieldname or ""
	return fn[len("custom_"):] if fn.startswith("custom_") else fn


def allowed_values(category, grain):
	"""The pickable human values of `category` visible to this grain — the controlled vocabulary
	a caller may send for a Link -> CRM Picklist Value field. Read-only; same grain rule as the
	Link query. Returns [value, ...] ordered as the picker shows them."""
	if not category:
		return []
	conds = {"category": category}
	conds.update(_grain_filters(grain))
	rows = frappe.get_all(
		"CRM Picklist Value", filters=conds, fields=["value"], order_by="position asc, value asc"
	)
	# de-dupe (a grain-specific + a global row can share a value) preserving order
	seen, out = set(), []
	for r in rows:
		if r.value not in seen:
			seen.add(r.value)
			out.append(r.value)
	return out


def resolve_to_pk(category, value, grain):
	"""A human `value` -> the CRM Picklist Value composite PK for `category` in this grain, or
	None if unmatched. The write-side twin of allowed_values (same master, same grain rule). When
	both a grain-specific and a global row share the value, the grain-specific one wins."""
	v = (value or "").strip()
	if not v or not category:
		return None
	conds = {"category": category, "value": v}
	conds.update(_grain_filters(grain))
	rows = frappe.get_all(
		"CRM Picklist Value", filters=conds, fields=["name", "vertical", "group", "program"]
	)
	if not rows:
		return None
	# Prefer the most grain-specific match (fewest blank/wildcard axes) when a grain-scoped and a
	# global row share the value. (Sorted in Python — Frappe rejects backticks in order_by, and
	# `group` is a reserved word.)
	rows.sort(key=lambda r: sum(1 for a in (r.vertical, r.get("group"), r.program) if not a))
	return rows[0].name


# --------------------------------------------------------------------------------------------
# GENERIC composite-PK Link seam. Every grain-scoped master whose PK is a composite `::` string
# (so a Link stores the PK, but callers think in the human value) registers ONE resolver + ONE
# value-lister here. resolve_row_links (ingestion) and the partner lead_schema (discovery) both
# dispatch through this registry — so adding a master = adding one entry, never a new code path,
# and discovery/ingestion for that master can't drift. Scope is bounded: only masters actually
# WRITABLE via ingestion need an entry (today: CRM Picklist Value + CRM Lead Stage; CRM City and
# the automation masters have no ingestion-writable Link, so they're intentionally absent).
# --------------------------------------------------------------------------------------------
def _stage_to_pk(value, grain, fieldname):
	"""A human stage ('New Lead') -> CRM Lead Stage PK 'program::stage', scoped to the lead's
	program (grain[2]). Stage names are unique per program, so program+stage is exact. None if no
	program or unmatched. (Selectability is NOT filtered here — validate_stage enforces that a
	pickable leaf was chosen; this only translates the value to its ID.)"""
	program = grain[2] if len(grain) > 2 else ""
	v = (value or "").strip()
	if not program or not v:
		return None
	return frappe.db.get_value("CRM Lead Stage", {"program": program, "stage": v}, "name")


def _stage_values(grain, fieldname):
	"""The selectable stage values for the lead's program — the controlled vocabulary a caller may
	send for a CRM Lead Stage Link."""
	program = grain[2] if len(grain) > 2 else ""
	if not program:
		return []
	rows = frappe.get_all(
		"CRM Lead Stage", filters={"program": program, "selectable": 1},
		fields=["stage"], order_by="position asc, stage asc",
	)
	return [r.stage for r in rows]


# master doctype -> (resolve value->PK, list allowed values). fieldname is passed so a resolver can
# derive per-field context (picklist category); grain is (vertical, group, program).
_COMPOSITE_PK_MASTERS = {
	"CRM Picklist Value": (
		lambda value, grain, fieldname: resolve_to_pk(category_of(fieldname), value, grain),
		lambda grain, fieldname: allowed_values(category_of(fieldname), grain),
	),
	"CRM Lead Stage": (_stage_to_pk, _stage_values),
}


def values_for(master, grain, fieldname):
	"""Discovery: the human values a caller may send for a Link at `master`, grain-scoped. [] for a
	master with no registered vocabulary (so callers can loop over every Link field blindly)."""
	entry = _COMPOSITE_PK_MASTERS.get(master)
	return entry[1](grain, fieldname) if entry else []


def resolve_row_links(doctype, row, grain):
	"""Return a copy of `row` (a fieldname->value dict) with every Link at a GRAIN-SCOPED COMPOSITE-PK
	master (registry above) translated from its human value to the composite PK. This is the INGESTION
	seam: the partner API and the intake fold both call it (via _upsert_one) BEFORE building the doc —
	resolution must happen here, not in a doc_event, because Frappe runs _validate_links() BEFORE any
	before_validate hook on insert/save. Mirrors migration.resolve.resolve_links — the one rule.

	  * a value already a valid PK is kept (idempotent — the Desk UI / a re-send stores PKs);
	  * an unmatched value is DROPPED (omitted) + logged, never written as an invalid Link (so one
	    bad value can't 500 the whole create — same fail-open-on-that-field policy as migration);
	  * Links at a non-registered master, non-strings, and blanks pass through unchanged."""
	meta = frappe.get_meta(doctype)
	out = {}
	for fn, val in row.items():
		f = meta.get_field(fn)
		entry = _COMPOSITE_PK_MASTERS.get(f.options) if (f and f.fieldtype == "Link") else None
		if not entry or not isinstance(val, str) or not val.strip() or frappe.db.exists(f.options, val):
			out[fn] = val  # pass through (non-composite-PK Link / blank / already a PK)
			continue
		pk = entry[0](val, grain, fn)
		if pk:
			out[fn] = pk
		else:
			frappe.logger("tatva_picklist").info(
				f"dropped unmatched {f.options} {doctype}.{fn}={val!r} for grain {grain}"
			)  # omit -> the field is left unset rather than an invalid Link
	return out
