# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class TatvaInvitationDomain(Document):
	pass  # one allowed or blocked invitation domain, checked by its parent (Tatva Platform Settings)
