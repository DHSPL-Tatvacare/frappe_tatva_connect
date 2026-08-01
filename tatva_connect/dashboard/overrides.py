# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""The two rules a `CRM Dashboard` obeys that no field can declare. Wired by override_doctype_class."""

import frappe
from crm.fcrm.doctype.crm_dashboard.crm_dashboard import CRMDashboard
from frappe import _
from frappe.utils import escape_html

from tatva_connect.dashboard import declaration, seed


class CRMDashboardOverride(CRMDashboard):
	def validate(self):
		self._assert_no_card_appears_twice()
		self._assert_exposed_filters()

	def on_update(self):
		declaration.retire_cache()

	def _assert_no_card_appears_twice(self):
		# A Link may repeat in a child table, and two tiles with one key is a grid the renderer cannot draw.
		seen, repeated = set(), []
		for row in self.get("charts") or []:
			if row.chart in seen:
				repeated.append(row.chart)
			seen.add(row.chart)
		if repeated:
			frappe.throw(
				_("{0} is on this dashboard more than once. One card, one place.").format(
					frappe.bold(escape_html(", ".join(sorted(set(repeated)))))
				),
				title=_("That card is already here"),
			)

	def _assert_exposed_filters(self):
		parsed = declaration.parsed(self.get("exposed_filters"), _("Exposed Filters"), list)
		# Asked of the seed, never restated here — a name we cannot apply would draw a control that does nothing.
		shipped = seed.filters_shipped()
		unknown = sorted(name for name in parsed if name not in shipped)
		if unknown:
			frappe.throw(
				_("{0} is not a filter this dashboard can apply. The ones it can are {1}.").format(
					frappe.bold(escape_html(", ".join(str(name) for name in unknown))),
					frappe.bold(", ".join(shipped)),
				),
				title=_("That filter does nothing"),
			)
