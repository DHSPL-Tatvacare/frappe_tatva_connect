"""Gated partner DEAL API: read and update the SALE, addressed by the customer it belongs to.

Shares the ONE brain in `tatva_connect.api._base`: the SAME `_resolve_caller` enablement gate (reached
through `partner._caller_fields`, so the caller's catalog grant is the same one lead_get and lead_update
honour), the SAME grain-scoped `resolve_lead`, the SAME `_ok`/`_fail` envelope, error codes, rate limit
and Idempotency-Key claim. Nothing here is a second authorisation path, a second envelope or a second
field curation.

IDENTITY. A partner addresses a customer by the LEAD, for life: `name` (the CRM Lead id lead_create
returned), `lead` (the word every other sub-resource uses for it), or `mobile_no`. A DEAL id is never
sent and never published. Conversion is a CRM event and is invisible from out here:
`deal.deals.one_deal_per_lead` allows a lead no second deal, so a lead resolves its deal unambiguously
and the address a partner already holds keeps working across it.

WHAT A DEAL CARRIES HERE. The sale, and nothing else. A sale line is a `CRM Products` row, and that
table is declared on BOTH the lead and the deal — so the line written with lead_update before conversion
is copied onto the deal by the CRM's own convert, and after conversion the same line is written here.
The patient's own facts are the lead's: they are read and written with lead_get / lead_update and are
not restated on this surface. A renewal or an upsell is a further ROW, never a second deal.

RENEWAL IS NEVER SENT. `custom_renewal_date` is the SKU's duration added to the row's start date, and
`deal.deals.stamp_renewal_dates` recomputes it on every save, so a value sent for it does not survive.
The SKU's duration and product line belong to `CRM Product` and are never part of a payload either.

  GET deal_get     -> the customer's sale, by lead `name` / `lead` or `mobile_no`
  PUT deal_update  -> upsert sale lines onto that customer's deal (lead_update's upsert-by-key contract)

There is no deal_create (a rep converts a lead; a partner does not), no deal_delete, and no bulk lane.
"""
import frappe
from frappe import _

from tatva_connect.api._base import (
	ACTION_FETCHED,
	ACTION_UPDATED,
	_api,
	_ok,
	resolve_lead,
	throw_field,
)
from tatva_connect.api.partner import (
	_apply_children,
	_caller_fields,
	_collect,
	_curate,
	_resolve_picklists,
)
from tatva_connect.taxonomy import grain

DOCTYPE = "CRM Deal"


# -- helpers -----------------------------------------------------------------

def _deal_children(child_allow):
	"""The caller's catalog sections that the DEAL itself carries — asked of both schemas, never named.

	A section qualifies when the deal declares the SAME Table field, at the SAME child doctype, as the
	lead does. That equality is also what makes the lead's own collector and writer the right ones to
	read those rows with, and it is why the sale line is the only section that reaches this surface."""
	deal_meta = frappe.get_meta(DOCTYPE)
	lead_meta = frappe.get_meta("CRM Lead")
	shared = {}
	for cf, allowed in child_allow.items():
		on_deal, on_lead = deal_meta.get_field(cf), lead_meta.get_field(cf)
		if on_deal and on_lead and on_deal.fieldtype == "Table" and on_deal.options == on_lead.options:
			shared[cf] = allowed
	return shared


def _resolve_deal(data, mp, is_sysmgr):
	"""(lead name, deal name) for the customer a payload addresses. The ONE lead->deal hop.

	The lead is resolved by the SAME grain-scoped `resolve_lead` every sub-resource uses, so missing and
	out-of-scope answer identically and no second scoping rule exists. The deal is then that lead's own,
	because `one_deal_per_lead` permits no other. `name` is accepted alongside `lead` so a caller holding
	the id lead_create returned addresses the customer here with the word it already stores it under."""
	lead_name = resolve_lead(mp, is_sysmgr, {
		"lead": data.get("lead") or data.get("name"),
		"mobile_no": data.get("mobile_no"),
	})
	deal_name = frappe.db.get_value(DOCTYPE, {"lead": lead_name}, "name")
	if not deal_name:
		# The lead resolved in scope, so naming it confirms nothing a lead_get would not already answer.
		throw_field(_(
			"Lead {0} has no deal yet, because a deal begins when the patient converts in the CRM. Send "
			"the sale with lead_update until then; the line is carried onto the deal at conversion."
		).format(lead_name), ["name", "mobile_no"], frappe.DoesNotExistError)
	return lead_name, deal_name


def _deal_view(doc, lead_name, child_allow):
	"""The partner-facing shape of a deal: the customer's sale, keyed on the lead that owns it.

	Built by the SAME `_curate` every lead response is built by, so one sale line reads identically
	whether it is fetched off the lead before conversion or off the deal after it. No parent fields are
	passed: the patient's facts are the lead's and lead_get answers them."""
	view = _curate(doc, [], child_allow)
	# conversion is invisible: the customer is addressed by the lead for life, so a deal id is never published
	view.pop("name", None)
	# the caller's own label was sent with the lead and is echoed there, not restated off a deal that has no column for it
	view.pop("external_id", None)
	view["lead"] = lead_name
	return view


# -- per-record core ---------------------------------------------------------

def _read_one(data, mp, is_sysmgr, child_allow):
	"""Load ONE customer's deal -> the partner view."""
	lead_name, deal_name = _resolve_deal(data, mp, is_sysmgr)
	return _deal_view(frappe.get_doc(DOCTYPE, deal_name), lead_name, child_allow)


def _update_one(data, mp, is_sysmgr, child_allow):
	"""Upsert the sale lines a payload carries onto that customer's deal. Returns (view, "updated")."""
	lead_name, deal_name = _resolve_deal(data, mp, is_sysmgr)
	doc = frappe.get_doc(DOCTYPE, deal_name)
	# No parent fields and no routing: every patient fact is the lead's, and the deal's grain is re-derived from that lead on every save.
	children = _collect(data, [], child_allow, allow_routing=False)[1]
	# The deal's OWN grain, asked of the grain brain, so a picklist value resolves against the line this customer is actually on.
	children = _resolve_picklists({}, children, grain.of(DOCTYPE, deal_name))[1]
	_apply_children(doc, children)
	doc.save(ignore_permissions=True)  # authz-ok: tier-b — gated by _resolve_caller + resolve_lead, before the save
	return _deal_view(doc, lead_name, child_allow), ACTION_UPDATED


# -- singular endpoints ------------------------------------------------------

@frappe.whitelist(methods=["GET"])
@_api(read=True)
def deal_get(**_kwargs):
	"""Read one customer's sale by lead `name` (or `lead`) or `mobile_no`, grain-scoped. A lead that has
	not converted answers a not-found naming that lead, never an empty record."""
	_user, mp, is_sysmgr, _parent_fields, child_allow = _caller_fields()
	_ok(action=ACTION_FETCHED,
	    data=_read_one(frappe.form_dict, mp, is_sysmgr, _deal_children(child_allow)))


@frappe.whitelist(methods=["PUT"])
@_api
def deal_update(**_kwargs):
	"""Write sale lines onto one customer's deal. Body: {name|lead|mobile_no, <sale table>:[{...}]}.

	Rows are upsert-by-key on the section's key field, exactly as lead_update writes them: a new key adds
	a row, the same key updates that row, a row the payload omits is left untouched, and a row is dropped
	with {<key field>, "_delete": true}. Retries are made safe with the Idempotency-Key header."""
	_user, mp, is_sysmgr, _parent_fields, child_allow = _caller_fields()
	view, action = _update_one(frappe.form_dict, mp, is_sysmgr, _deal_children(child_allow))
	_ok(action=action, data=view)
