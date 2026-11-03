# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class CRMTelephonyExemptRole(Document):
	"""One role whose holders are credited on every lead of a grain that keeps outgoing calls to the lead's own reps."""

	pass
