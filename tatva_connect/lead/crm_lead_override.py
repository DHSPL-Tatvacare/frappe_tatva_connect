# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""CRM Lead class override — the ONLY reason it exists: add `parse_list_data` so the SPA list
renders composite-`::`-PK Link columns (custom_stage / custom_substage) as their clean
display_label, never the raw `program::stage` PK. crm's get_list_data calls
`get_controller("CRM Lead").parse_list_data(rows)` (a @staticmethod) after fetching — but stock
CRM Lead has none, so the raw PK reaches the UI.

Reuses the ONE display-label brain (lead.detail._display_label = the target doctype's title_field)
that the Data tab (DetailPanel) and the header stage pill already use — no ::-parsing, no second
rule. Purely additive: subclasses crm's CRMLead and changes nothing else, so the doctype is 100%
stock behaviour plus this one projection helper (invariant A.1 order: override_doctype_class,
not a fork)."""
import frappe
from crm.fcrm.doctype.crm_lead.crm_lead import CRMLead

from tatva_connect.lead.detail import _display_label

# CRM Lead Link fields whose stored value is a grain composite `::` PK — must show display_label
# in a list. (custom_group/custom_current_program are simple PKs with no title_field → already
# clean, so they're intentionally NOT listed.)
_LABEL_FIELDS = ("custom_stage", "custom_substage")


class TatvaCRMLead(CRMLead):
	@staticmethod
	def parse_list_data(leads):
		"""Replace each composite-PK Link value with its display_label (title_field) for display.
		Idempotent-ish: a value that doesn't resolve is left untouched. Rows are plain dicts."""
		if not leads:
			return leads
		meta = frappe.get_meta("CRM Lead")
		dfs = {fn: meta.get_field(fn) for fn in _LABEL_FIELDS}
		for row in leads:
			for fn, df in dfs.items():
				val = row.get(fn)
				if val:
					label = _display_label(df, val)
					if label:
						row[fn] = label
		return leads
