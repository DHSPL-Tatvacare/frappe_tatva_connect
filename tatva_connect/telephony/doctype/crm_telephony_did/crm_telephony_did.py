# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from tatva_connect.telephony import envelope as env


class CRMTelephonyDID(Document):
	"""A number that is owned, and the grain its calls belong to.

	The row is named by the last-10 digits of the number. Providers send the same DID two ways —
	'9240276210' on an IVR call, '+919240276210' on a Dialer call — so resolution has to normalize
	regardless, and naming the row by the normalized key turns the lookup into a primary-key hit
	instead of a LIKE scan on every inbound call.
	"""

	def autoname(self):
		digits = env.phone_digits(self.did_number)
		if not digits:
			frappe.throw(
				_("{0} is not a full phone number. A DID needs at least {1} digits.").format(
					frappe.bold(self.did_number or ""), env.PHONE_MIN_DIGITS
				),
				title=_("Invalid DID"),
			)
		self.name = digits

	def validate(self):
		# A DID with no grain would pass the relevance gate but attribute its calls to nothing.
		if not (self.vertical or self.psp_group or self.program):
			frappe.throw(
				_("Set at least one of Product Line / Group / Program. A DID with no grain cannot "
				  "attribute a call to a lead."),
				title=_("Invalid DID mapping"),
			)
