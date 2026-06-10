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

from tatva_connect.whatsapp import api as wati


class WATIWhatsAppMessage(WhatsAppMessage):
	def set_whatsapp_account(self):
		"""Pick the WATI account by the lead's taxonomy (Program > Group > Product Line).

		No global default: if nothing is set and no routing rule matches, we raise
		rather than fall back to a default account — so a lead can never be sent
		through the wrong tenant. Inbound rows arrive with the account already
		stamped (by the webhook), so this is a no-op for them.
		"""
		if self.whatsapp_account:
			return
		from tatva_connect.whatsapp import routing

		account = routing.resolve_for_message(self)
		if not account:
			frappe.throw(
				_(
					"No WATI Account Routing rule matches this lead's Product Line / Group / "
					"Program. Configure WATI Account Routing before sending."
				),
				title=_("No WATI route"),
			)
		self.whatsapp_account = account

	def before_insert(self):
		# Capture the lead this OUTBOUND row was explicitly filed under, BEFORE crm's
		# validate (which runs after before_insert) rewrites reference_name to the first
		# lead by phone. Restored in before_save. The send itself happens in super's
		# before_insert and uses the correct account (resolved here, pre-clobber).
		# Inbound attribution is handled separately (webhook.pin_inbound_reference).
		if (self.type or "") == "Outgoing" and self.reference_doctype and self.reference_name:
			self.flags.tatva_intended_ref = (self.reference_doctype, self.reference_name)
		super().before_insert()

	def before_save(self):
		# Undo crm.api.whatsapp.validate's clobber for outbound rows on a shared phone:
		# restore the lead the sender filed it under so the sent message renders in the
		# right lead's tab. Runs after crm's validate, before the row is written.
		intended = self.flags.get("tatva_intended_ref")
		if intended and (self.type or "") == "Outgoing":
			self.reference_doctype, self.reference_name = intended

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
		# Session message: an attachment (media) or free-text.
		number = wati.normalize_number(self.to)
		if self.attach and self.content_type in ("document", "image", "video", "audio"):
			resp = self._wati_send_attachment(account, number)
		else:
			resp = wati.send_session_message(account, number, self.message or "")
		self._wati_apply_response(resp)

	def _wati_send_attachment(self, account, number):
		"""Send the row's attachment through WATI (the file itself, not its name)."""
		import mimetypes

		caption = self.message or ""
		if self.attach.startswith("http"):
			return wati.send_session_file_via_url(account, number, self.attach, caption)
		# Local Frappe file -> read bytes and upload multipart (works for private files too).
		file_doc = frappe.get_doc("File", {"file_url": self.attach})
		filename = file_doc.file_name or self.attach.split("/")[-1]
		mimetype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
		return wati.send_session_file(account, number, filename, file_doc.get_content(), mimetype, caption)

	def send_template(self):
		account = self._wati_account()
		if account is None:
			return super().send_template()
		wati.assert_wati(account)
		wati.assert_enabled()
		template = frappe.get_doc("WhatsApp Templates", self.template)
		params = self._wati_body_parameters(template)
		# Save the resolved values so the CRM WhatsApp tab renders {{N}} filled
		# (crm substitutes the display from template_parameters).
		if params:
			self.template_parameters = json.dumps([p["value"] for p in params])
		resp = wati.send_template_message(
			account,
			to_number=wati.normalize_number(self.to),
			template_name=template.actual_name or template.template_name,
			broadcast_name=f"crm_{frappe.scrub(self.template)}",
			parameters=params,
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

	def send_read_receipt(self):
		"""No-Meta backstop: upstream POSTs a read receipt to Meta's Graph API.

		WATI has no read-receipt endpoint on our contract, so for a WATI account
		this is a no-op — never fall through to super() (which would reach Meta).
		"""
		if self._wati_account() is not None:
			return None
		return super().send_read_receipt()

	# --- helpers ---
	def _wati_body_parameters(self, template):
		"""Resolve template body placeholders into WATI's [{name, value}] shape.

		Mirrors upstream value-resolution (body_param JSON, flags.custom_ref_doc,
		or the reference doc), named positionally ("1","2",...). Empty for a
		static-body template.
		"""
		# Primary path: explicit body_param keyed by the real {{N}} index.
		# Preserve the original key as the WATI param name — do NOT renumber to
		# 1..N, or a template with non-contiguous slots ({{1}},{{3}}) sends the
		# wrong value. Send "" for an empty slot rather than dropping it (dropping
		# would shift every later slot).
		if self.body_param:
			try:
				bp = json.loads(self.body_param)
			except Exception:
				return []
			return [
				{"name": str(k), "value": "" if bp[k] is None else str(bp[k])}
				for k in sorted(bp, key=lambda x: int(x))
			]
		# Fallback: positional values resolved from field_names (notification /
		# automated path). field_names is ordered to match {{1}},{{2}},… by the
		# operator, so positional naming is correct here.
		field_names = (template.field_names or "").split(",") if template.field_names else []
		if not field_names:
			return []
		if self.flags.get("custom_ref_doc"):
			cv = self.flags.custom_ref_doc
			values = [cv.get(fn.strip()) for fn in field_names]
		elif self.reference_doctype and self.reference_name:
			ref = frappe.get_doc(self.reference_doctype, self.reference_name)
			values = [ref.get_formatted(fn.strip()) for fn in field_names]
		else:
			return []
		return [
			{"name": str(i + 1), "value": "" if v is None else str(v)}
			for i, v in enumerate(values)
		]

	def _wati_apply_response(self, resp):
		"""Map a WATI send response onto the row. Success -> store the message id.

		WATI is inconsistent across endpoints: template send returns
		{"result": true}; session-file send returns {"result": "<id-string>"}
		(no `ok` key); some session endpoints add {"ok": true}. Errors come back
		as {"result": false, ...} / {"ok": false, ...} (HTTP 200) or as a body our
		`api._post` normalised to {"result": false, "info": ...} on a 4xx/timeout.

		Rule: an explicit false flag, a non-dict, or an empty/falsy `result` with
		no truthy `ok` is a failure; anything else succeeded.
		"""
		if not isinstance(resp, dict):
			self.status = "failed"
			frappe.throw(_("WATI send failed: {0}").format(str(resp)[:400]), title=_("WATI Error"))

		ok = resp.get("ok")
		result = resp.get("result")
		failed = (
			ok is False
			or result is False
			or (ok is not True and result in (None, "", "false", "False", 0))
		)
		if failed:
			info = resp.get("info")
			if not info and isinstance(resp.get("message"), str):
				info = resp.get("message")
			self.status = "failed"
			frappe.throw(
				_("WATI send failed: {0}").format(info or json.dumps(resp)),
				title=_("WATI Error"),
			)

		msg = resp.get("message") if isinstance(resp.get("message"), dict) else {}
		message_id = (
			resp.get("local_message_id")
			or msg.get("localMessageId")
			or msg.get("whatsappMessageId")
			# file sends return the id as the `result` string itself.
			or (result if isinstance(result, str) and result not in ("true", "false") else None)
		)
		if message_id:
			self.message_id = message_id
		self.status = "sent"
