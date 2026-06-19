"""WATI override of frappe_whatsapp's `WhatsApp Message` (Seam 1).

The CRM lead WhatsApp tab and all of crm/api/whatsapp.py write rows to the
`WhatsApp Message` doctype; `before_insert` builds a Meta payload and POSTs to
Meta via `notify()`. We subclass the controller and route sends through WATI.

No-Meta guarantee (guardrails 1, 3, 5):
- builders (`send_template`/`send_outgoing`) send via WATI;
- every send resolves the provider adapter (providers.adapter_for); unknown provider raises;
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

from tatva_connect.whatsapp import providers


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

	def _provider_account(self):
		"""Return the linked WhatsApp Account doc iff it maps to a registered provider
		adapter, else None (then the stock frappe_whatsapp Meta path runs). The adapter
		that speaks this account's provider is resolved via providers.adapter_for."""
		if not self.whatsapp_account:
			return None
		account = frappe.get_cached_doc("WhatsApp Account", self.whatsapp_account)
		return account if providers.has_adapter(account) else None

	# --- send seams (override the builders, not notify) ---
	def send_outgoing(self):
		# Ingested mirror: this Outgoing row records a message that already exists on WATI
		# (typed in the WATI portal, or pulled by the history backfill) — it must NEVER be
		# re-sent. The webhook/backfill set this flag before insert; honour it before any
		# provider call (guardrail: a received message can't loop back out).
		if self.flags.get("tatva_ingested"):
			return
		account = self._provider_account()
		if account is None:
			return super().send_outgoing()
		if self.type != "Outgoing":
			return
		adapter = providers.adapter_for(account)
		adapter.assert_enabled()
		if self.message_type == "Template":
			# before_insert sets message_type=Template when a template is chosen;
			# don't re-send rows that already carry a message_id (retries / notif inserts).
			if not self.message_id:
				self.send_template()
			return
		# Session message: an attachment (media) or free-text.
		number = adapter.normalize_number(self.to)
		if self.attach and self.content_type in ("document", "image", "video", "audio"):
			resp = self._send_attachment(account, number)
		else:
			resp = adapter.send_session_message(account, number, self.message or "")
		self._apply_send_response(resp)
		if self.attach and self.content_type in ("document", "image", "video", "audio") \
		   and self.reference_doctype == "CRM Lead" and self.reference_name:
			from tatva_connect.whatsapp import media as media_module
			self.attach = media_module.adopt_outbound_media(self.attach, self.reference_name, self.message_id)

	def _send_attachment(self, account, number):
		"""Send the row's attachment through the provider (the file itself, not its name).

		Our own File rows (incl. Azure-backed proxy URLs) always send as BYTES — the
		provider cannot authenticate to our proxy URL, so a URL send would fail. Only a
		genuine external link (not one of our File rows) uses the URL path.
		"""
		import mimetypes

		adapter = providers.adapter_for(account)
		caption = self.message or ""
		filedoc = frappe.db.exists("File", {"file_url": self.attach})
		if filedoc:
			fd = frappe.get_doc("File", filedoc)
			filename = fd.file_name or self.attach.split("/")[-1]
			mimetype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
			return adapter.send_session_file(account, number, filename, fd.get_content(), mimetype, caption)
		# Not one of our File rows -> a true external URL.
		return adapter.send_session_file_via_url(account, number, self.attach, caption)

	def send_template(self):
		account = self._provider_account()
		if account is None:
			return super().send_template()
		adapter = providers.adapter_for(account)
		adapter.assert_enabled()
		template = frappe.get_doc("WhatsApp Templates", self.template)
		params = self._wati_body_parameters(template)
		# Save the resolved values so the CRM WhatsApp tab renders {{N}} filled
		# (crm substitutes the display from template_parameters).
		if params:
			self.template_parameters = json.dumps([p["value"] for p in params])
		resp = adapter.send_template_message(
			account,
			to_number=adapter.normalize_number(self.to),
			template_name=template.actual_name or template.template_name,
			# Campaign label from the clean template name (not the account-scoped record id),
			# so WATI shows e.g. "crm_bcatechissue" and same-template sends group cleanly.
			broadcast_name=f"crm_{frappe.scrub(template.actual_name or template.template_name)}",
			parameters=params,
		)
		self._apply_send_response(resp)

	def notify(self, data):
		"""Backstop (guardrail #5): a provider-backed account must never reach Meta's notify().

		Our builders send via the resolved provider adapter and never call this for an
		adapter-backed account. If some other code path does, fail loud rather than POST to Meta.
		"""
		account = self._provider_account()
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

		WATI has no read-receipt endpoint on our contract, so for an adapter-backed
		account this is a no-op — never fall through to super() (which would reach Meta).
		"""
		if self._provider_account() is not None:
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
		"""Ordered provider parameter names — single source of truth in the adapter."""
		return providers.adapter_for(self._provider_account()).template_param_names(template)

	def _apply_send_response(self, resp):
		"""Map a provider send response onto the row. Manual-send side of the shared contract:
		classify once (adapter.classify_send_response — the single source of truth, also used by
		the notification path), then apply this path's side-effect: throw on failure (which
		rolls back the insert), else stamp the message id and mark sent."""
		r = providers.adapter_for(self._provider_account()).classify_send_response(resp)
		if r.failed:
			self.status = "failed"
			frappe.throw(
				_("WATI send failed: {0}").format(r.reason or _("message could not be sent")),
				title=_("WATI Error"),
			)
		if r.message_id:
			self.message_id = r.message_id
		self.status = "sent"
