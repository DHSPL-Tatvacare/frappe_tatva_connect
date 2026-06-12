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
		if self.attach and self.content_type in ("document", "image", "video", "audio") \
		   and self.reference_doctype == "CRM Lead" and self.reference_name:
			from tatva_connect.whatsapp import media as media_module
			self.attach = media_module.adopt_outbound_media(self.attach, self.reference_name, self.message_id)

	def _wati_send_attachment(self, account, number):
		"""Send the row's attachment through WATI (the file itself, not its name).

		Our own File rows (incl. Azure-backed proxy URLs) always send as BYTES — WATI
		cannot authenticate to our proxy URL, so a URL send would fail. Only a genuine
		external link (not one of our File rows) uses the URL path.
		"""
		import mimetypes

		caption = self.message or ""
		filedoc = frappe.db.exists("File", {"file_url": self.attach})
		if filedoc:
			fd = frappe.get_doc("File", filedoc)
			filename = fd.file_name or self.attach.split("/")[-1]
			mimetype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
			return wati.send_session_file(account, number, filename, fd.get_content(), mimetype, caption)
		# Not one of our File rows -> a true external URL.
		return wati.send_session_file_via_url(account, number, self.attach, caption)

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
			# Campaign label from the clean template name (not the account-scoped record id),
			# so WATI shows e.g. "crm_bcatechissue" and same-template sends group cleanly.
			broadcast_name=f"crm_{frappe.scrub(template.actual_name or template.template_name)}",
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

		WATI matches params by NAME (the template's paramName), NOT by the {{N}}
		position shown in the body — sending positional "1","2" makes WATI fill the
		slots blank ("...cannot have typos or blank text"). The real names live in
		sample_values keys, in body order (built from WATI customParams by
		templates_sync). Empty for a static-body template.
		"""
		names = self._wati_param_names(template)

		def _name(idx):  # idx = the 1-based {{N}} slot
			return names[idx - 1] if 0 < idx <= len(names) else str(idx)

		# Primary path: explicit body_param keyed by the real {{N}} index. Send ""
		# for an empty slot rather than dropping it (dropping would shift later slots).
		if self.body_param:
			try:
				bp = json.loads(self.body_param)
			except Exception:
				return []
			return [
				{"name": _name(int(k)), "value": "" if bp[k] is None else str(bp[k])}
				for k in sorted(bp, key=lambda x: int(x))
			]
		# Fallback: positional values resolved from field_names (notification /
		# automated path). field_names is ordered to match {{1}},{{2}},… by the operator.
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
			{"name": _name(i + 1), "value": "" if v is None else str(v)}
			for i, v in enumerate(values)
		]

	def _wati_param_names(self, template):
		"""Ordered WATI parameter names for {{1}},{{2}},… — the keys of sample_values
		(WATI customParams in body order; see templates_sync). Empty if none; callers
		fall back to the positional index, which also matches sample_values when a
		template's params were unnamed (sync keys them "1","2",… in that case)."""
		try:
			sv = json.loads(template.sample_values) if template.sample_values else {}
			return list(sv.keys())
		except Exception:
			return []

	def _wati_apply_response(self, resp):
		"""Map a WATI send response onto the row. Manual-send side of the shared contract:
		classify once (wati.classify_send_response — the single source of truth, also used by
		the notification path), then apply this path's side-effect: throw on failure (which
		rolls back the insert), else stamp the message id and mark sent."""
		r = wati.classify_send_response(resp)
		if r.failed:
			self.status = "failed"
			frappe.throw(
				_("WATI send failed: {0}").format(r.reason or _("message could not be sent")),
				title=_("WATI Error"),
			)
		if r.message_id:
			self.message_id = r.message_id
		self.status = "sent"
