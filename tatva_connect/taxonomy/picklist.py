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
	(blank-axis rows are wildcards visible in every grain.)"""
	out = {}
	for col, val in zip(_AXES, grain):
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

	conds = {"category": category, "display_label": ["like", "%{0}%".format(txt)]}
	conds.update(_grain_filters(grain))

	# Cascading picklist: when the form supplies the parent field's current value, keep only
	# options that match it OR declare no dependency. The parent value is just a value test on
	# rows already clamped to the entitled grain, so it can't widen scope.
	dep_field = (f.depends_on_field or "").strip()
	dep_value = (f.depends_on_value or "").strip()
	if dep_field:
		conds["depends_on_field"] = ["in", [dep_field, ""]]
		conds["depends_on_value"] = ["in", [dep_value, ""]]

	return frappe.get_all(
		"CRM Picklist Value",
		filters=conds,
		fields=["name", "display_label"],
		order_by="position asc, display_label asc",
		limit=min(cint(page_len) or 20, _CAP),
		as_list=True,
	)
