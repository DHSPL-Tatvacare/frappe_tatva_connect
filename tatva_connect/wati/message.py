"""WATI override of frappe_whatsapp's `WhatsApp Message` (Seam 1).

The CRM lead WhatsApp tab and all of crm/api/whatsapp.py write rows to the
`WhatsApp Message` doctype; `before_insert` builds a Meta payload and POSTs to
Meta via `notify()`. We subclass the controller and route sends through WATI.

No-Meta guarantee (guardrails 1, 3, 5):
- builders (`send_template`/`send_outgoing`) send via WATI;
- every send boundary calls `wati.assert_wati(account)` — non-WATI raises;
- `notify()` is overridden as a backstop: if any un-anticipated path calls it
  on a WATI account, we raise instead of letting it reach Meta.

Registered via `override_doctype_class` in hooks.py.
"""
import json

import frappe
from frappe import _
from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_message.whatsapp_message import (
	WhatsAppMessage,
)

from tatva_connect.wati import api as wati


class WATIWhatsAppMessage(WhatsAppMessage):
	def _wati_account(self):
		"""Return the linked WhatsApp Account doc iff it's a WATI tenant, else None."""
		if not self.whatsapp_account:
			return None
		account = frappe.get_cached_doc("WhatsApp Account", self.whatsapp_account)
		return account if wati.is_wati_account(account) else None

	# --- send seams (override the builders, not notify) ---
	def send_outgoing(self):
		account = self._wati_account()
		if account is None:
			return super().send_outgoing()
		if self.type != "Outgoing":
			return
		wati.assert_wati(account)
		wati.assert_enabled()
		if self.message_type == "Template":
			# before_insert sets message_type=Template when a template is chosen;
			# don't re-send rows that already carry a message_id (retries / notif inserts).
			if not self.message_id:
				self.send_template()
			return
		# Free-text / session message
		resp = wati.send_session_message(account, wati.normalize_number(self.to), self.message or "")
		self._wati_apply_response(resp)

	def send_template(self):
		account = self._wati_account()
		if account is None:
			return super().send_template()
		wati.assert_wati(account)
		wati.assert_enabled()
		template = frappe.get_doc("WhatsApp Templates", self.template)
		resp = wati.send_template_message(
			account,
			to_number=wati.normalize_number(self.to),
			template_name=template.actual_name or template.template_name,
			broadcast_name=f"crm_{frappe.scrub(self.template)}",
			parameters=self._wati_body_parameters(template),
		)
		self._wati_apply_response(resp)

	def notify(self, data):
		"""Backstop (guardrail #5): a WATI account must never reach Meta's notify().

		Our builders send via WATI and never call this for WATI accounts. If some
		other code path does, fail loud rather than POST to Meta.
		"""
		account = self._wati_account()
		if account is not None:
			frappe.throw(
				_("Blocked a Meta-bound send on WATI account '{0}'. WATI is the only transport.").format(
					self.whatsapp_account
				),
				title=_("Blocked non-WATI send"),
			)
		return super().notify(data)

	# --- helpers ---
	def _wati_body_parameters(self, template):
		"""Resolve template body placeholders into WATI's [{name, value}] shape.

		Mirrors upstream value-resolution (body_param JSON, flags.custom_ref_doc,
		or the reference doc), named positionally ("1","2",...). Empty for a
		static-body template.
		"""
		values = []
		field_names = (template.field_names or "").split(",") if template.field_names else []
		if self.body_param:
			try:
				values = list(json.loads(self.body_param).values())
			except Exception:
				values = []
		elif self.flags.get("custom_ref_doc") and field_names:
			cv = self.flags.custom_ref_doc
			values = [cv.get(fn.strip()) for fn in field_names]
		elif self.reference_doctype and self.reference_name and field_names:
			ref = frappe.get_doc(self.reference_doctype, self.reference_name)
			values = [ref.get_formatted(fn.strip()) for fn in field_names]
		return [
			{"name": str(i + 1), "value": v}
			for i, v in enumerate(values)
			if v is not None
		]

	def _wati_apply_response(self, resp):
		"""WATI returns HTTP 200 with {"result": bool, ...}; map onto the row.

		Success -> store local_message_id (status webhooks thread on its camelCase
		form). result:false (e.g. "Not enough credits") -> raise, status=Failed.
		"""
		if not isinstance(resp, dict) or not resp.get("result"):
			info = resp.get("info") if isinstance(resp, dict) else None
			self.status = "Failed"
			frappe.throw(
				_("WATI send failed: {0}").format(info or json.dumps(resp)),
				title=_("WATI Error"),
			)
		message_id = resp.get("local_message_id") or (resp.get("message") or {}).get("localMessageId")
		if message_id:
			self.message_id = message_id
		self.status = "Success"
