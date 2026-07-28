# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A transcription service, as an account row — the same shape every other inbound producer takes.

A service that transcribes our recordings and posts the text back is not a new kind of integration and
does not get a new kind of authentication. It is an account: a per-account webhook token, optional HMAC,
optional IP allowlist, a raw delivery log and a replay button, all of it the shared spine's, all of it
already proven by the WhatsApp, voice and telephony channels.

The row is the ONLY thing that names the service. The webhook URL carries no vendor segment, so moving
to a different transcriber is configuration rather than a deploy.
"""
from frappe.model.document import Document

from tatva_connect.taxonomy.normalize import normalize_field
from tatva_connect.webhooks import ingress


class CRMTranscriptionAccount(Document):
	def validate(self):
		normalize_field(self, "account_name")
		ingress.assert_ingress_config(self)

	def on_update(self):
		# The digest that makes token authentication one indexed read is derived here, never entered.
		ingress.sync_token_digests(self)
