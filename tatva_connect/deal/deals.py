"""Deal guards — a Deal is the customer a Lead became, so it is anchored to that lead and grained by it.

The grain is DERIVED from the lead on every save, never typed: a deal that drifted off its lead's product
line would be scoped to a population it does not belong to. The axis columns are asked of the grain brain
rather than restated, so a new axis reaches the deal without a second edit here. Every entry point is gated
by one dormant switch; off, a Deal behaves exactly as stock crm ships it.

A deal is BORN at the one stage its programme declares to be the moment the lead bought, and that judgement
is made once, at birth. What follows is arithmetic: a sale line's renewal date is the plan's duration added
to the line's start date, so it is computed here and never typed.
"""
import frappe
from frappe import _
from frappe.utils import add_days, cint

from tatva_connect import automation
from tatva_connect.api._base import throw_field
from tatva_connect.taxonomy import grain
from tatva_connect.whatsapp.phone import to_e164

_GATE = "Deal::CRM Deal::guards"

# The Deal's own phone columns; the lead's extra caregiver/alternate numbers have no home here.
PHONE_FIELDS = ("mobile_no", "phone")


def require_lead(doc, method=None):
	"""A Deal names the lead it came from. Without one it has no patient, no product line and no gate."""
	if not automation.is_enabled(_GATE):
		return
	if not doc.get("lead"):
		throw_field(
			_("A deal must be created from a lead."), ["lead"], title=_("No lead on this deal")
		)


def _axis_pairs():
	"""(deal column, lead column) for every axis both carry — both tuples come back in `grain.AXES` order."""
	return [
		(deal_column, lead_column)
		for deal_column, lead_column in zip(
			grain.columns("CRM Deal"), grain.columns("CRM Lead"), strict=True
		)
		if deal_column and lead_column
	]


def stamp_grain_from_lead(doc, method=None):
	"""Copy the lead's grain onto the deal on every save, so the two can never disagree."""
	if not automation.is_enabled(_GATE):
		return
	if not doc.get("lead"):
		return
	pairs = _axis_pairs()
	if not pairs:
		return
	row = frappe.db.get_value("CRM Lead", doc.lead, [c for _, c in pairs], as_dict=True) or {}
	for deal_column, lead_column in pairs:
		doc.set(deal_column, row.get(lead_column))


def guard_deals_enabled(doc, method=None):
	"""Deals are a per-product-line surface: a line that has not been armed for them refuses to carry one."""
	if not automation.is_enabled(_GATE):
		return
	column = grain.columns("CRM Deal")[grain.AXES.index("vertical")]
	vertical = doc.get(column) if column else None
	if not vertical:
		return
	if not frappe.db.get_value("CRM Vertical", vertical, "deals_enabled"):
		throw_field(
			_("Deals are not enabled for {0}.").format(vertical),
			["lead"],
			title=_("Deals not enabled"),
		)


def one_deal_per_lead(doc, method=None):
	"""One deal per customer: a renewal or an upsell is a row inside the deal, never a second deal."""
	if not automation.is_enabled(_GATE):
		return
	if not doc.get("lead"):
		return
	existing = frappe.db.get_value(
		"CRM Deal", {"lead": doc.lead, "name": ["!=", doc.name or ""]}, "name"
	)
	if existing:
		throw_field(
			_("This lead already has a deal ({0}). Add the sale to that deal instead.").format(existing),
			["lead"],
			title=_("Duplicate deal"),
		)


def normalize_deal_phones(doc, method=None):
	"""Bring the deal's phone fields to their stored +E.164 form, through the one shared parser."""
	if not automation.is_enabled(_GATE):
		return
	meta = frappe.get_meta(doc.doctype)
	for f in PHONE_FIELDS:
		val = doc.get(f)
		if val:
			label = (meta.get_field(f) or frappe._dict()).label or f
			doc.set(f, to_e164(val, fieldname=label))


def guard_conversion_point(doc, method=None):
	"""A lead becomes a customer only from the stage its programme declares as the moment they bought."""
	if not automation.is_enabled(_GATE):
		return
	# birth-only: converting is an event, and re-judging it on every later save would freeze every historic deal
	if not doc.is_new() or not doc.get("lead"):
		return
	stage = frappe.db.get_value("CRM Lead", doc.lead, "custom_substage")
	if not stage or not frappe.db.get_value("CRM Lead Stage", stage, "is_conversion_point"):
		throw_field(
			_("This lead is not at the stage where a deal can be created."),
			["lead"],
			title=_("Not ready to convert"),
		)


def stamp_renewal_dates(doc, method=None):
	"""Renewal is arithmetic, never a typed date: the SKU's duration applied to the row's start date."""
	if not automation.is_enabled(_GATE):
		return
	for row in doc.get("products") or []:
		# written on EVERY pass: the field is read-only, so a skipped row would keep a date its inputs no longer support
		days = (
			frappe.db.get_value("CRM Product", row.product_code, "custom_duration_days")
			if row.get("product_code")
			else None
		)
		row.custom_renewal_date = (
			add_days(row.custom_start_date, cint(days)) if row.get("custom_start_date") and days else None
		)
