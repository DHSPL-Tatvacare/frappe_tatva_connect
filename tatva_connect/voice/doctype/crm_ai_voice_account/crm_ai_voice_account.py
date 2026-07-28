# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
from frappe.model.document import Document

from tatva_connect.taxonomy.normalize import normalize_field
from tatva_connect.webhooks import ingress


class CRMAIVoiceAccount(Document):
	def validate(self):
		# The display name is normalised; the api_key/base_url/from_phone are machine values, never touched.
		normalize_field(self, "account_name")
		ingress.assert_ingress_config(self)

	def on_update(self):
		# The same webhook-ingress contract the WhatsApp and Telephony accounts carry — the digest that
		# makes token authentication one indexed read is derived here, never entered.
		ingress.sync_token_digests(self)
