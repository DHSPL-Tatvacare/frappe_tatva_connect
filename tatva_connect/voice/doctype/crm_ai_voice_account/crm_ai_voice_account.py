# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
import frappe
from frappe import _
from frappe.model.document import Document

from tatva_connect.taxonomy.normalize import normalize_field
from tatva_connect.webhooks import ingress
from tatva_connect.whatsapp.phone import to_e164


class CRMAIVoiceAccount(Document):
	def validate(self):
		# The display name is normalised; the api_key/base_url are machine values, never touched.
		normalize_field(self, "account_name")
		self._conform_from_phone()
		ingress.assert_ingress_config(self)

	def _conform_from_phone(self):
		"""The caller-id is STORED CANONICAL, and it was the one number in this app that was not.

		`to_number` is conformed on the way out by the provider's declared `number_format`
		(`sends.send_voice`); `from_phone` was only `.strip()`ed, so `08035303509` reached the provider as
		typed and the call failed or dialled from a caller-id nobody chose. This file recorded that as a
		decision — it assumed the operator knew the format, and the form said nothing.

		`to_e164` is the STORE job of `phone.py`'s three (MATCH · STORE · SEND), the same brain
		`lead.leads:174` conforms a patient's number with. No phone logic of its own lives here.
		"""
		raw = (self.from_phone or "").strip()
		if not raw:
			return
		try:
			self.from_phone = to_e164(raw, fieldname=_("From Number"))
		except frappe.ValidationError:
			# `to_e164` names the number but not the shape wanted, and the form is where that was missing.
			frappe.throw(
				_("{0} is not a number this account can dial from. Give the caller-id in full international "
				  "form, starting with the country code — for example +919876543210.").format(raw),
				title=_("Invalid From Number"),
			)

	def on_update(self):
		# The same webhook-ingress contract the WhatsApp and Telephony accounts carry — the digest that
		# makes token authentication one indexed read is derived here, never entered.
		ingress.sync_token_digests(self)
