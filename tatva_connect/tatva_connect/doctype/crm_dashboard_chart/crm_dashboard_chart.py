# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""One dashboard card, declared as a row. A chart is not a function anywhere in this app.

WHY A ROW AND NOT A FUNCTION. A hand-written chart query is a second place that decides who may see
which records, and it decides it wrong the first time somebody forgets. Here the row names a list, a
column and a filter, and `dashboard/executor.py` turns that into ONE `frappe.get_list` call — so the row
gate, the User Permission grain and every `permission_query_conditions` hook apply to a card exactly as
they apply to the list behind it. There is no way to declare an ungated chart because there is no way to
declare a query at all.

REFUSAL AT SAVE IS THE WHOLE SAFETY STORY, as it is for `CRM Derived Field`. A chart that names a column
the list does not have fails at READ time, on a rep's dashboard, as one dead card among nine live ones —
so every rule the executor relies on is asserted here, at the one moment an operator is present to read
the message. What is checked is exactly what the executor assumes and nothing more.

The group-by column is checked hardest, because it is load-bearing twice: it is what the figure is broken
down by AND what the drill-down filters on. A column reached through a link can be shown but cannot be
filtered on, which is why traversal belongs to `label_field` alone.

Plan: docs/plans/2026-07-31-dashboard-role-layouts-phase-1.md
"""

import frappe
from frappe import _
from frappe.model import default_fields
from frappe.model.document import Document
from frappe.utils import escape_html

from tatva_connect.dashboard import declaration

# A donut and a bar are one figure broken down by a column; a number card is the figure alone.
_GROUPED = ("donut", "bar")


class CRMDashboardChart(Document):
	"""One declared card. Everything the executor assumes about it is proved before it can be saved."""

	def on_update(self):
		declaration.retire_cache()

	def validate(self):
		self.chart_name = (self.chart_name or "").strip()
		self.group_by_field = (self.group_by_field or "").strip()
		self.label_field = (self.label_field or "").strip()
		self.date_field = (self.date_field or "").strip()
		self.aggregate_field = (self.aggregate_field or "").strip()
		# A blank required field, and a Select outside its own options, are both frappe's message to give
		# (_validate_selects runs the moment this returns); ours would only obscure them.
		if not (self.chart_name and self.label and self.chart_type and self.source_doctype):
			return
		if not frappe.db.exists("DocType", self.source_doctype):
			return
		self._assert_shape_matches_type()
		self._assert_group_by_is_a_real_column()
		self._assert_date_field_is_a_real_column()
		self._assert_aggregate_is_measurable()
		self._assert_base_filters_shape()

	def _assert_shape_matches_type(self):
		"""A number card has nothing to break down, and a donut with nothing to break down is one slice."""
		if self.chart_type == "number" and self.group_by_field:
			frappe.throw(
				_(
					"A number card is a single figure, so it cannot be grouped by {0}. Clear Group By Field, or make this a donut or a bar."
				).format(frappe.bold(escape_html(self.group_by_field))),
				title=_("A number card has no groups"),
			)
		if self.chart_type in _GROUPED and not self.group_by_field:
			frappe.throw(
				_("A {0} is one figure broken down by a column, so it needs a Group By Field.").format(
					frappe.bold(self.chart_type)
				),
				title=_("There is nothing to break down"),
			)

	def _assert_group_by_is_a_real_column(self):
		"""The grouped column is what the drill-down filters on, so it has to be one the list can filter."""
		if not self.group_by_field:
			return
		if "." in self.group_by_field:
			frappe.throw(
				_(
					"{0} reaches through a link, and the drill-down filters on the grouped column itself — so it has to be a column of {1}. Group by the link column and put {0} in Label Field to show it."
				).format(frappe.bold(escape_html(self.group_by_field)), frappe.bold(self.source_doctype)),
				title=_("That column belongs to another record"),
			)
		self._assert_column(self.group_by_field, _("grouped by"))

	def _assert_date_field_is_a_real_column(self):
		if self.date_field:
			self._assert_column(self.date_field, _("dated by"))

	def _assert_aggregate_is_measurable(self):
		"""COUNT counts records and needs no column; SUM and AVG have to be told which one."""
		if self.aggregate == "COUNT":
			return
		if not self.aggregate_field:
			frappe.throw(
				_("{0} adds up a column, so it needs one named in Aggregate Field.").format(
					frappe.bold(self.aggregate)
				),
				title=_("There is nothing to add up"),
			)
		self._assert_column(self.aggregate_field, _("measured by"))

	def _assert_base_filters_shape(self):
		"""What is typed here is byte for byte what `frappe.get_list(filters=...)` is handed, so it is an
		object of column to condition and nothing else."""
		declaration.parsed(self.base_filters, _("Base Filters"), dict)

	def _assert_column(self, fieldname, role):
		meta = frappe.get_meta(self.source_doctype)
		if fieldname in default_fields or meta.get_field(fieldname):
			return
		frappe.throw(
			_("{0} has no column called {1}, so a card cannot be {2} it.").format(
				frappe.bold(self.source_doctype), frappe.bold(escape_html(fieldname)), role
			),
			title=_("That column does not exist"),
		)
