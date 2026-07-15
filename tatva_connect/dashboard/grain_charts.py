# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Grain-scoped lead charts for the CRM SPA "Manager Dashboard".

The CRM dashboard framework (crm.api.dashboard.get_dashboard) dispatches each layout item to a
`get_<name>` in the fork; those are thin `# TATVA` shims that delegate here, so the fork holds UI +
dispatch only and the logic lives in this app.

Stages are program-scoped: CRM Lead Stage is keyed `{program}::{stage}`, so a funnel is coherent only
WITHIN one program. Labels come from CRM Lead Stage.display_label (the bare stage, never the `::`
composite key) and order from .position — the SAME contract taxonomy/picklist.py reads.

Source of truth is `custom_substage` (the leaf the rep picks). The parent field `custom_stage` is
derived and is NOT reliably populated on bulk/migrated leads, so "Leads by Stage" rolls the substage up
to its parent via `substage_of` rather than reading the empty `custom_stage`.
"""
import frappe
from frappe import _
from frappe.query_builder import Case, DocType
from frappe.query_builder.functions import Coalesce, Count, Date


def _range(from_date, to_date):
	"""Default an unset range to the current month, mirroring the fork's dashboard functions."""
	if not from_date or not to_date:
		from_date = frappe.utils.get_first_day(from_date or frappe.utils.nowdate())
		to_date = frappe.utils.get_last_day(to_date or frappe.utils.nowdate())
	return from_date, to_date


def _axis(data, title, subtitle):
	"""The fork's axis_chart contract (mirror of get_funnel_conversion) — horizontal bars, one per stage."""
	return {
		"data": data or [],
		"title": title,
		"subtitle": subtitle,
		"xAxis": {"title": _("Stage"), "key": "stage", "type": "category"},
		"yAxis": {"title": _("Leads")},
		"swapXY": True,
		"series": [{"name": "count", "type": "bar", "echartOptions": {"colorBy": "data"}}],
	}


def _program_label(program):
	return frappe.db.get_value("CRM Program", program, "program_name") or program


def leads_by_vertical(from_date=None, to_date=None, user=None):
	"""Leads grouped by Product Line (custom_vertical). CRM Vertical is named by vertical_name, so the
	group value is already a clean label — no join needed."""
	from_date, to_date = _range(from_date, to_date)
	Lead = DocType("CRM Lead")
	query = (
		frappe.qb.from_(Lead)
		.select(Coalesce(Lead.custom_vertical, _("Unassigned")).as_("vertical"), Count("*").as_("count"))
		.where(Date(Lead.creation).between(from_date, to_date))
		.groupby(Lead.custom_vertical)
		.orderby(Count("*"), order=frappe.qb.desc)
	)
	if user:
		query = query.where(Lead.lead_owner == user)
	return {
		"data": query.run(as_dict=True) or [],
		"title": _("Leads by Product Line"),
		"subtitle": _("Distribution across grains"),
		"categoryColumn": "vertical",
		"valueColumn": "count",
	}


def leads_by_substage(from_date=None, to_date=None, user=None, vertical=None, program=None):
	"""Leads grouped by the leaf sub-stage the rep set (custom_substage), scoped to one program."""
	from_date, to_date = _range(from_date, to_date)
	if not program:
		return _axis([], _("Leads by Sub-stage"), _("Select a program to view the sub-stage funnel"))

	Lead = DocType("CRM Lead")
	Stage = DocType("CRM Lead Stage")
	query = (
		frappe.qb.from_(Lead)
		.join(Stage)
		.on(Lead.custom_substage == Stage.name)
		.select(Stage.display_label.as_("stage"), Count("*").as_("count"))
		.where(Date(Lead.creation).between(from_date, to_date))
		.where(Stage.program == program)
		.groupby(Stage.name, Stage.display_label, Stage.position)
		.orderby(Stage.position)
	)
	if user:
		query = query.where(Lead.lead_owner == user)
	if vertical:
		query = query.where(Lead.custom_vertical == vertical)
	return _axis(query.run(as_dict=True), _("Leads by Sub-stage"), _("Program: {0}").format(_program_label(program)))


def leads_by_stage(from_date=None, to_date=None, user=None, vertical=None, program=None):
	"""Leads grouped by their top-level stage, DERIVED from the sub-stage the rep set (substage_of, or
	the value itself when it is already a top-level stage) — custom_stage is not reliably populated.

	Two aliased joins on CRM Lead Stage: `sub` = the leaf on the lead, `par` = its parent (or itself).
	Built entirely with the query builder — every value is a bound parameter, no string interpolation."""
	from_date, to_date = _range(from_date, to_date)
	if not program:
		return _axis([], _("Leads by Stage"), _("Select a program to view the stage funnel"))

	Lead = DocType("CRM Lead")
	Sub = DocType("CRM Lead Stage").as_("sub")
	Par = DocType("CRM Lead Stage").as_("par")
	parent_key = Case().when((Sub.substage_of.isnull()) | (Sub.substage_of == ""), Sub.name).else_(Sub.substage_of)

	query = (
		frappe.qb.from_(Lead)
		.join(Sub)
		.on(Lead.custom_substage == Sub.name)
		.join(Par)
		.on(parent_key == Par.name)
		.select(Par.display_label.as_("stage"), Count("*").as_("count"))
		.where(Date(Lead.creation).between(from_date, to_date))
		.where(Sub.program == program)
		.groupby(Par.name, Par.display_label, Par.position)
		.orderby(Par.position)
	)
	if user:
		query = query.where(Lead.lead_owner == user)
	if vertical:
		query = query.where(Lead.custom_vertical == vertical)
	return _axis(query.run(as_dict=True), _("Leads by Stage"), _("Program: {0}").format(_program_label(program)))
