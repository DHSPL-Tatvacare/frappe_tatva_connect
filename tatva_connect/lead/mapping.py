# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The ONE answer to "which catalogue fields may this map into, and into which section".

Every surface that lets an operator point something at a lead field asks here: the Intake Form builder,
the Facebook question mapper, and the Desk bulk import. Before this module they asked it twice by two
routes, which is how a second brain grows — and the catalogue rule that a row naming no live column must
never be offered existed on only one of them.

The two SCOPE SOURCES are deliberately not collapsed, because they are not the same question:

  `contract=` — what may THIS contract write. The ticks are the contract's own, and ingestion reads the
                same set, so what an operator can pick and what a lead can carry are one answer.
  `grain=`    — what is visible to this grain internally. Asked by author-time surfaces that carry axes
                but no contract yet: an unsaved Intake builder narrows its list the moment a grain is
                picked, with no save and no reject-after-the-fact.

Everything else is shared and lives only here: the catalogue is read once, the section resolves its own
target doctype, and a row whose fieldname names no live column is dropped.
"""
import frappe
from frappe import _

from tatva_connect.access import entitlement
from tatva_connect.lead_sync.contract import allowed_field_keys

_CATALOG = "CRM Lead API Field"
_SECTION = "CRM Lead Section"


def mappable_sections():
	"""The sections a mapper may target, in the seed's own display order — the brain, never a literal."""
	return frappe.get_all(_SECTION, fields=["section_key", "title", "display_order"],
	                      order_by="display_order asc")


def mappable_fields(*, section=None, contract=None, grain=None):
	"""Catalogue fields a mapper may offer: [{field_key, section, fieldname, label, fieldtype}].

	Scope by `contract` when one is held, else by `grain` (a 3-tuple of axes). One of the two is
	required — answering an unscoped call would hand back the whole catalogue.
	"""
	if contract is None and grain is None:
		frappe.throw(_("A contract or a grain is required to scope a field list."), title=_("Unscoped"))

	rows = frappe.get_all(_CATALOG, filters={"section": section} if section else {},
	                      fields=["field_key", "section", "fieldname", "label"],
	                      order_by="section asc, label asc")
	permitted = _permitted(rows, contract, grain)
	targets = {s.section_key: s.target_doctype
	           for s in frappe.get_all(_SECTION, fields=["section_key", "target_doctype"])}

	out = []
	for row in rows:
		if row.field_key not in permitted:
			continue
		df = _live_column(targets.get(row.section), row.fieldname)
		if df is None:
			continue  # catalogued but not a live data column — never offer what could never be written
		out.append({"field_key": row.field_key, "section": row.section, "fieldname": row.fieldname,
		            "label": _(row.label or df.label or row.fieldname), "fieldtype": df.fieldtype})
	return out


def _permitted(rows, contract, grain):
	"""The permitted key set — a contract's own ticks, or internal visibility for a grain."""
	if contract is not None:
		return allowed_field_keys(contract)
	axes = tuple((a or "").strip() for a in grain)
	return {r.field_key for r in rows if entitlement.field_in_grains_via_contract(r.field_key, [axes])}


def _live_column(target_doctype, fieldname):
	"""The docfield a catalogue row names, or None when it names no column a value could land in."""
	if not target_doctype:
		return None
	df = frappe.get_meta(target_doctype).get_field(fieldname)
	if not df or df.fieldtype in frappe.model.no_value_fields:
		return None
	return df
