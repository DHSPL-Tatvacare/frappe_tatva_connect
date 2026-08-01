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

Plan: docs/plans/2026-07-31-dashboard-role-layouts-phase-1.md
"""

import frappe
from frappe import _
from frappe.model import default_fields
from frappe.model.document import Document
from frappe.utils import escape_html

from tatva_connect.dashboard import declaration
from tatva_connect.list_engine import derived


class CRMDashboardChart(Document):
	"""One declared card. Everything the executor assumes about it is proved before it can be saved."""

	def on_update(self):
		declaration.retire_cache()

	def validate(self):
		self.chart_name = (self.chart_name or "").strip()
		self.group_by_field = (self.group_by_field or "").strip()
		self.split_by = (self.split_by or "").strip()
		self.date_field = (self.date_field or "").strip()
		self.aggregate_field = (self.aggregate_field or "").strip()
		# A blank reqd field and an out-of-options Select are frappe's messages to give, not ours to obscure.
		if not (self.chart_name and self.label and self.chart_type and self.source_doctype):
			return
		if not frappe.db.exists("DocType", self.source_doctype):
			return
		self._assert_shape_matches_type()
		self._assert_group_by_is_a_real_column()
		self._assert_split_by_is_a_second_dimension()
		self._assert_time_bucket_has_a_date()
		self._assert_date_field_is_a_real_column()
		self._assert_aggregate_is_measurable()
		self._assert_base_filters_shape()

	@property
	def _bucketed(self):
		return self.time_bucket == declaration.MONTH

	def _assert_shape_matches_type(self):
		"""A number card has nothing to break down, and a donut with nothing to break down is one slice."""
		if self.chart_type == "number" and self.group_by_field:
			frappe.throw(
				_(
					"A number card is a single figure, so it cannot be grouped by {0}. Clear Group By Field, or make this a donut or a bar."
				).format(frappe.bold(escape_html(self.group_by_field))),
				title=_("A number card has no groups"),
			)
		if self.chart_type == "number" and (self.split_by or self._bucketed):
			frappe.throw(
				_(
					"A number card is a single figure, so it has no second dimension and no time buckets. Clear Split By and Time Bucket, or make this a card that breaks down."
				),
				title=_("A number card has no groups"),
			)
		if self.chart_type in declaration.GROUPED and not (self.group_by_field or self._bucketed):
			frappe.throw(
				_(
					"A {0} is one figure broken down along an axis, so it needs a Group By Field or a Time Bucket."
				).format(frappe.bold(self.chart_type)),
				title=_("There is nothing to break down"),
			)
		if self.chart_type == declaration.CROSSED and not self.split_by:
			frappe.throw(
				_("A {0} is two dimensions crossed, so it needs a Split By column beside its axis.").format(
					frappe.bold(self.chart_type)
				),
				title=_("There is nothing to cross"),
			)

	def _assert_group_by_is_a_real_column(self):
		"""The grouped column is what the drill-down filters on, so it has to be one the list can filter.

		A DERIVED field is the one thing it may be instead: it is no column at all, and the executor counts
		it a bucket at a time rather than grouping on it, so the column rule is not the rule it answers to."""
		if not self.group_by_field:
			return
		if "." in self.group_by_field:
			frappe.throw(
				_(
					"{0} reaches through a link, and the drill-down filters on the grouped column itself — so it has to be a column of {1}. Group by the link column and put {0} in Label Field to show it."
				).format(frappe.bold(escape_html(self.group_by_field)), frappe.bold(self.source_doctype)),
				title=_("That column belongs to another record"),
			)
		if derived.get(self.source_doctype, self.group_by_field):
			return
		self._assert_column(self.group_by_field, _("grouped by"))

	def _assert_split_by_is_a_second_dimension(self):
		"""The split is a series AND half of the drill filter, so it obeys the grouped column's rule exactly.

		Both dimensions come out of ONE grouped query, and a derived field is not a column that query can
		name on either side of the cross — so a card may be GROUPED by one, and never split across one."""
		if not self.split_by:
			return
		if "." in self.split_by:
			frappe.throw(
				_(
					"{0} reaches through a link, and a click on a cell filters on the split column itself — so it has to be a column of {1}."
				).format(frappe.bold(escape_html(self.split_by)), frappe.bold(self.source_doctype)),
				title=_("That column belongs to another record"),
			)
		crossed = next(
			(
				name
				for name in (self.split_by, self.group_by_field)
				if name and derived.get(self.source_doctype, name)
			),
			None,
		)
		if crossed:
			frappe.throw(
				_(
					"{0} is a derived field, which is counted one bucket at a time rather than grouped — so a card can be grouped by it, but never crossed with a second dimension."
				).format(frappe.bold(escape_html(crossed))),
				title=_("That column is derived"),
			)
		if self.split_by == self.group_by_field:
			frappe.throw(
				_("{0} is already what this card is grouped by, so splitting by it is one dimension twice.").format(
					frappe.bold(escape_html(self.split_by))
				),
				title=_("That is the same dimension"),
			)
		self._assert_column(self.split_by, _("split by"))

	def _assert_time_bucket_has_a_date(self):
		"""A month bucket is the MONTH of a date column, so there has to be one to take the month of."""
		if self._bucketed and not self.date_field:
			frappe.throw(
				_("A card bucketed by {0} takes the month of a date column, so it needs a Date Field.").format(
					frappe.bold(self.time_bucket)
				),
				title=_("There is no date to bucket"),
			)

	def _assert_date_field_is_a_real_column(self):
		if self.date_field:
			self._assert_column(self.date_field, _("dated by"))

	def _assert_aggregate_is_measurable(self):
		"""COUNT counts records and needs no column; every other aggregate has to be told which one."""
		if self.aggregate == "COUNT":
			return
		if self.aggregate == declaration.DISTINCT and self.chart_type != "number":
			frappe.throw(
				_(
					"How many different values a column holds is a single figure counted as a number of groups, so it belongs on a number card."
				),
				title=_("That aggregate has no breakdown"),
			)
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
