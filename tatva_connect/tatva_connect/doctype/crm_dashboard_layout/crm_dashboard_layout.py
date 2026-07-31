# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""One role's dashboard: which cards, where, and which filter controls above them.

A LAYOUT STORES POINTERS, NEVER CONTENT (CLAUDE.md -> I5). The only thing said about a card here is its
name; everything the card IS lives in its own `CRM Dashboard Chart` row. So a card corrected once is
corrected on every dashboard that shows it, and there is never a layout carrying a stale copy of a
definition somebody has since fixed. That is why the missing-chart check below refuses at Save: the
pointer is the whole of the relationship, and a pointer at nothing is the one way this shape can break.

Priority is not decoration. Every real role profile on this site grants several roles at once, so a person
matches more than one layout as a matter of course — `dashboard/resolver.py` picks the highest priority
and that is the ONE answer. Without it the dashboard somebody sees would depend on row order.

Plan: docs/plans/2026-07-31-dashboard-role-layouts-phase-1.md
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import escape_html

from tatva_connect.dashboard import declaration, executor

# What one placement declares. A key nothing reads would be stored and then silently ignored.
_PLACEMENT_KEYS = ("chart", "x", "y", "w", "h")



class CRMDashboardLayout(Document):
	"""One role's dashboard. Every card it names is proved to exist before the row can be saved."""

	def on_update(self):
		declaration.retire_cache()

	def validate(self):
		placements = self._placements()
		self._assert_charts_exist(placements)
		self._assert_no_card_appears_twice(placements)
		self._assert_exposed_filters()

	def _placements(self):
		"""The layout read as a list of placements, checked where a reader would otherwise read past it."""
		parsed = declaration.parsed(self.layout, _("Layout"), list)
		for placement in parsed:
			if not isinstance(placement, dict):
				frappe.throw(
					_("{0} is not a card. A card is {1}.").format(
						frappe.bold(escape_html(str(placement))),
						frappe.bold('{"chart": "total_leads", "x": 0, "y": 0, "w": 3, "h": 2}'),
					),
					title=_("That is not a card"),
				)
			missing = [key for key in _PLACEMENT_KEYS if placement.get(key) is None]
			if missing:
				frappe.throw(
					_("{0} does not say {1}. Every card names a chart and where it sits.").format(
						frappe.bold(escape_html(frappe.as_json(placement))),
						frappe.bold(escape_html(", ".join(missing))),
					),
					title=_("A card is incomplete"),
				)
		return parsed

	def _assert_charts_exist(self, placements):
		"""A layout points at cards. A pointer at nothing draws a dead tile nobody can explain."""
		named = [placement["chart"] for placement in placements]
		if not named:
			return
		rows = frappe.get_list(
			declaration.CHART,
			filters={"name": ["in", named]},
			fields=["name"],
			limit=len(named),
			ignore_permissions=True,  # authz-ok: tier-c — resolves the operator's own layout pointers at save, reads no user data
		)
		existing = {row["name"] for row in rows}
		unknown = sorted(set(named) - existing)
		if unknown:
			frappe.throw(
				_("There is no Dashboard Chart called {0}, so this dashboard would show an empty tile.").format(
					frappe.bold(escape_html(", ".join(unknown)))
				),
				title=_("That card does not exist"),
			)

	def _assert_no_card_appears_twice(self, placements):
		seen, repeated = set(), []
		for placement in placements:
			if placement["chart"] in seen:
				repeated.append(placement["chart"])
			seen.add(placement["chart"])
		if repeated:
			frappe.throw(
				_("{0} is on this dashboard more than once. One card, one place.").format(
					frappe.bold(escape_html(", ".join(sorted(set(repeated)))))
				),
				title=_("That card is already here"),
			)

	def _assert_exposed_filters(self):
		parsed = declaration.parsed(self.exposed_filters, _("Exposed Filters"), list)
		# Asked of the executor, never restated here — a fifth name would draw a control that does nothing.
		unknown = sorted(name for name in parsed if name not in executor.KNOWN_FILTERS)
		if unknown:
			frappe.throw(
				_("{0} is not a filter this dashboard can apply. The ones it can are {1}.").format(
					frappe.bold(escape_html(", ".join(str(name) for name in unknown))),
					frappe.bold(", ".join(executor.KNOWN_FILTERS)),
				),
				title=_("That filter does nothing"),
			)
