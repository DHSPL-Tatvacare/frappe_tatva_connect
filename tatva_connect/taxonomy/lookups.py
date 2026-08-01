"""Scoped link-queries for the taxonomy masters (guest-safe, read-only).

Lives in the taxonomy module beside the masters it serves (CRM City / Hospital / Doctor)
and their shared logic (`normalize`, `program_mode`). These are Frappe Link CUSTOM
QUERIES — a form wires one per field:

    field.get_query = () => ({ query: 'tatva_connect.taxonomy.lookups.<x>_query', filters: {...} })

The SCOPE is enforced HERE, server-side — each method only ever returns names inside the
caller's scope (state / grain / hospital), needs 2+ chars, and is capped. Because the Link
points at a custom query, the master doctypes do NOT need to be guest-readable: this method
is the single controlled door, so the full master can never be enumerated through the
generic resource API. The Link stores the row PK; the intake processor resolves PK -> human
name (see intake._link_label) before it lands on the lead.
"""
import frappe
from frappe import _
from frappe.utils import cint

from tatva_connect.taxonomy import grain

_CAP = 50
_MIN = 2


def _scoped(doctype, display_field, txt, scope, page_len):
	"""Shared scoped search: (name, display) rows where display LIKE txt, within `scope`.
	Returns [] unless every scope value is present and txt has 2+ chars."""
	if not all(scope.values()):
		return []
	txt = (txt or "").strip()
	if len(txt) < _MIN:
		return []
	filters = dict(scope)
	filters[display_field] = ["like", f"%{txt}%"]
	return frappe.get_all(
		doctype,
		filters=filters,
		fields=["name", display_field],
		order_by=f"{display_field} asc",
		limit=min(cint(page_len) or 20, _CAP),
		as_list=True,
	)


def _filters(filters):
	"""Link queries may hand `filters` as a dict or a JSON string — normalize to a dict."""
	if isinstance(filters, str):
		filters = frappe.parse_json(filters)
	return frappe._dict(filters or {})


@frappe.whitelist(allow_guest=True)  # guest-ok: public web-form autocomplete, read-only, server-scoped; never trusts a client grain (A.9)
def city_query(doctype, txt, searchfield, start, page_len, filters):
	"""Cities within the picked state. filters: {state}. (State -> City cascade.)"""
	f = _filters(filters)
	return _scoped("CRM City", "city_name", txt, {"state": (f.state or "").strip()}, page_len)


def _grain_from_form(filters):
	"""Derive the grain from the intake FORM the caller names — NEVER from client-supplied grain
	values. Mirrors frappe's web_form.get_link_options: the named form must exist AND be enabled
	(published), else PermissionError. The browser can only name a form; the SERVER owns the grain,
	read straight from the CRM Intake Form config. This is what stops cross-grain enumeration: the
	`program`/`group`/`vertical` a scraper sends is simply never read."""
	form = (_filters(filters).get("intake_form") or "").strip()
	enabled = frappe.db.get_value("CRM Intake Form", form, "enabled") if form else None
	if not enabled:
		frappe.throw(_("This form is not available."), frappe.PermissionError)
	return dict(zip(grain.AXES, grain.of("CRM Intake Form", form), strict=True))


@frappe.whitelist(allow_guest=True)  # guest-ok: public web-form autocomplete, read-only, server-scoped; never trusts a client grain (A.9)
def hospital_query(doctype, txt, searchfield, start, page_len, filters):
	"""Hospitals within the FORM's grain. filters: {intake_form}. The grain is looked up from the
	named CRM Intake Form server-side (frappe's web-form pattern) — any grain values a browser sends
	are ignored, so a guest cannot enumerate another grain by tampering with the filters."""
	return _scoped("CRM Hospital", "hospital_name", txt, _grain_from_form(filters), page_len)


@frappe.whitelist(allow_guest=True)  # guest-ok: public web-form autocomplete, read-only, server-scoped; never trusts a client grain (A.9)
def doctor_query(doctype, txt, searchfield, start, page_len, filters):
	"""Doctors at the picked hospital (the Doctor->Hospital FK). filters: {hospital} (its PK).
	Since the hospital PK already encodes the grain, this inherits the grain scope too."""
	f = _filters(filters)
	return _scoped("CRM Doctor", "doctor_name", txt, {"hospital": (f.hospital or "").strip()}, page_len)
