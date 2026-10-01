# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class CRMPushSettings(Document):
	def on_update(self):
		from tatva_connect.notifications import sender

		frappe.cache.delete_value(sender._ACCESS_TOKEN_CACHE_KEY)  # a new key must not ride the old key's OAuth token for 50 minutes
