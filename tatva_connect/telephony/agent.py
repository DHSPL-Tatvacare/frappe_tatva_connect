"""crm's CRM Telephony Agent, plus the one rule its Extensions table needs. Wired by override_doctype_class."""
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
		# One extension per account on this rep, and no other rep holding it on that account.
		mine = {}
		for row in self.get(resolve.SEAT_FIELD) or []:
			row.extension = (row.extension or "").strip()
			if row.telephony_account in mine:
				frappe.throw(
					_("{0} has more than one extension on {1}. Keep one.").format(self.name, row.telephony_account),
					title=_("Duplicate extension"),
				)
			mine[row.telephony_account] = row.extension
		if not mine:
			return
		for held in frappe.get_all(
			resolve.SEAT_CHILD,
			filters={"parenttype": self.doctype, "parent": ["!=", self.name], "extension": ["in", list(mine.values())]},
			fields=["parent", "telephony_account", "extension"],
		):
			if mine.get(held.telephony_account) == held.extension:
				frappe.throw(
					_("Extension {0} on {1} already belongs to {2}.").format(held.extension, held.telephony_account, held.parent),
					title=_("Extension in use"),
				)
