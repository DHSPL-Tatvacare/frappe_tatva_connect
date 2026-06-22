# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class CRMPayment(Document):
	"""A Razorpay payment against a CRM Lead — the activity engine's only structured
	sub-entity (design AUTHORITY §5/§6). Plain record; routing + dedup live in the
	migration loader (load.py::load_payments). No business logic here by design."""
	pass
