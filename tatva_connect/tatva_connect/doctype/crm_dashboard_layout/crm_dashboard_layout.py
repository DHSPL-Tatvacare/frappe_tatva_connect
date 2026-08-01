# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""One role's dashboard: which cards, where, and which filter controls above them.

A layout POINTS AT cards; it never carries one (CLAUDE.md -> I5). Each placement is a child row whose
`chart` is a Link, so frappe refuses a pointer at nothing before this file runs — there is no existence
check here because there is nothing left to check.

Priority is not decoration: a role profile grants several roles at once, so a person matches more than one
layout as a matter of course and `dashboard/resolver.py` takes the highest.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import escape_html

from tatva_connect.dashboard import declaration, executor


class CRMDashboardLayout(Document):
	def on_update(self):
		declaration.retire_cache()

	def validate(self):
		self._assert_no_card_appears_twice()
		self._assert_exposed_filters()

	def _assert_no_card_appears_twice(self):
		"""The one rule frappe has no native form for: a Link may repeat in a child table."""
		seen, repeated = set(), []
		for row in self.charts:
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
