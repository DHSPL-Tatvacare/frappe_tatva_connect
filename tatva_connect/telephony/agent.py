"""crm's CRM Telephony Agent, plus the one rule its Extensions table needs. Wired by override_doctype_class."""
from itertools import combinations

import frappe
from crm.fcrm.doctype.crm_telephony_agent.crm_telephony_agent import CRMTelephonyAgent
from frappe import _

from tatva_connect.telephony import cache, resolve


class TatvaTelephonyAgent(CRMTelephonyAgent):
	def validate(self):
		super().validate()
		self._assert_one_rep_per_extension()

	def on_update(self):
		cache.invalidate()

	def on_trash(self):
		cache.invalidate()

	def _assert_one_rep_per_extension(self):
		# One extension per account on this rep at a time, and no other rep holding it on that account over the same dates.
		rows = self.get(resolve.SEAT_FIELD) or []
		resolve.assert_dated(rows)
		for row in rows:
			row.extension = (row.extension or "").strip()
		for a, b in combinations(rows, 2):
			if a.telephony_account == b.telephony_account and resolve.overlaps(a, b):
				frappe.throw(
					_("{0} has more than one extension on {1} at the same time. Set Valid To on the one handed on.").format(self.name, a.telephony_account),
					title=_("Duplicate extension"),
				)
		if not rows:
			return
		for held in frappe.get_all(
			resolve.SEAT_CHILD,
			filters={"parenttype": self.doctype, "parent": ["!=", self.name], "extension": ["in", [r.extension for r in rows]]},
			fields=["parent", "telephony_account", "extension", "valid_from", "valid_to"],
		):
			for row in rows:
				if (row.telephony_account, row.extension) == (held.telephony_account, held.extension) and resolve.overlaps(row, held):
					frappe.throw(
						_("Extension {0} on {1} already belongs to {2} over these dates. Set Valid To on their row, or Valid From on this one.").format(held.extension, held.telephony_account, held.parent),
						title=_("Extension in use"),
					)

