# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

from frappe.model.document import Document

from tatva_connect.taxonomy.normalize import normalize_field
from tatva_connect.webhooks import ingress


class CRMTelephonyAccount(Document):
	def validate(self):
		# M-2: normalize the account display name. Code/secret fields (agent_number,
		# caller_id, base_url, tokens) are NOT normalized — they are machine values.
		normalize_field(self, "account_name")
		ingress.assert_ingress_config(self)

	def on_update(self):
		# The same webhook-ingress contract the upstream WhatsApp Account gets from its class override.
		ingress.sync_token_digests(self)
