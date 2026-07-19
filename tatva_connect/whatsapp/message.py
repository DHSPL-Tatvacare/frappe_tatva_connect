"""Channel override of frappe_whatsapp's `WhatsApp Message` (Seam 1).

The CRM lead WhatsApp tab and all of crm/api/whatsapp.py write rows to the `WhatsApp Message`
doctype; upstream's `before_insert` builds a Meta payload and POSTs to Meta via `notify()`. We
subclass the controller and route every send through the account's own channel adapter.

No-Meta guarantee (guardrails 1, 3, 5):
- the builders (`send_template`/`send_outgoing`) send through the adapter;
- every send resolves it from the account (channels.resolve.adapter_for); an unregistered provider raises;
- `notify()` is overridden as a backstop: if any un-anticipated path calls it on an adapter-backed
  account, we raise instead of letting it reach Meta.

Registered via `override_doctype_class` in hooks.py.
"""
import frappe
from frappe import _
from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_message.whatsapp_message import (
	WhatsAppMessage,
)

from tatva_connect.channels import resolve
from tatva_connect.taxonomy import labels
from tatva_connect.whatsapp import channel


class ChannelWhatsAppMessage(WhatsAppMessage):
	def set_whatsapp_account(self):
		"""Pick the account by the lead's taxonomy (Program > Group > Product Line).

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
					"No CRM WhatsApp Routing rule matches this lead's Product Line / Group / "
					"Program. Configure CRM WhatsApp Routing before sending."
				),
				title=_("No WhatsApp route"),
			)
		self.whatsapp_account = account

	def before_insert(self):
		# Capture the lead this OUTBOUND row was explicitly filed under, BEFORE crm's validate (which runs after before_insert) rewrites reference_name to the first lead by phone. Restored in before_save. The send itself happens in super's before_insert and uses the correct account (resolved here, pre-clobber). Inbound attribution is handled separately (webhook.pin_inbound_reference).
		if (self.type or "") == "Outgoing" and self.reference_doctype and self.reference_name:
			self.flags.tatva_intended_ref = (self.reference_doctype, self.reference_name)
		super().before_insert()

	def before_save(self):
		# Undo crm.api.whatsapp.validate's clobber for outbound rows on a shared phone: restore the lead the sender filed it under so the sent message renders in the right lead's tab. Runs after crm's validate, before the row is written.
		intended = self.flags.get("tatva_intended_ref")
		if intended and (self.type or "") == "Outgoing":
			self.reference_doctype, self.reference_name = intended

	def _channel_account(self):
		"""The linked WhatsApp Account doc iff it maps to a registered adapter, else None (then the
		stock frappe_whatsapp Meta path runs)."""
		if not self.whatsapp_account:
			return None
		account = frappe.get_cached_doc("WhatsApp Account", self.whatsapp_account)
		return account if resolve.has_adapter(account) else None

	# --- send seams (override the builders, not notify) ---
	def send_outgoing(self):
		# Ingested mirror: this Outgoing row records a message that already exists on the provider (typed in its portal, or pulled by the history backfill) — it must NEVER be re-sent. The webhook/backfill set this flag before insert; honour it before any provider call (guardrail: a received message can't loop back out).
		if self.flags.get("tatva_ingested"):
			return
		account = self._channel_account()
		if account is None:
			super().send_outgoing()
			return
		if self.type != "Outgoing":
			return
		channel.assert_enabled()
		adapter = resolve.adapter_for(account)
		if self.message_type == "Template":
			# before_insert sets message_type=Template when a template is chosen; don't re-send rows that already carry a message_id (retries / notif inserts).
			if not self.message_id:
				self.send_template()
			return
		# Session message: an attachment (media) or free-text.
		if self.attach and self.content_type in ("document", "image", "video", "audio"):
			result = self._send_attachment(account, adapter)
		else:
			result = adapter.send_session(account, self.to, self.message or "")
		self._apply_send_result(result)
		if self.attach and self.content_type in ("document", "image", "video", "audio") \
		   and self.reference_doctype == "CRM Lead" and self.reference_name:
			from tatva_connect.whatsapp import media as media_module
			self.attach = media_module.adopt_outbound_media(self.attach, self.reference_name, self.message_id)

	def _send_attachment(self, account, adapter):
		"""Send the row's attachment through the provider (the file itself, not its name).

		Our own File rows (incl. Azure-backed proxy URLs) always send as BYTES — the provider cannot
		authenticate to our proxy URL, so a URL send would fail. Only a genuine external link (not one
		of our File rows) uses the URL path.
		"""
		import mimetypes

		caption = self.message or ""
		filedoc = frappe.db.exists("File", {"file_url": self.attach})
		if filedoc:
			fd = frappe.get_doc("File", filedoc)
			filename = fd.file_name or self.attach.split("/")[-1]
			mimetype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
			return adapter.send_media(account, self.to, filename, fd.get_content(), mimetype, caption)
		# Not one of our File rows -> a true external URL.
		return adapter.send_media_url(account, self.to, self.attach, caption)

	def send_template(self):
		account = self._channel_account()
		if account is None:
			super().send_template()
			return
		channel.assert_enabled()
		adapter = resolve.adapter_for(account)
		template = frappe.get_doc("WhatsApp Templates", self.template)
		variables = self._body_parameters(template, adapter, account)
		# Save the resolved values so the CRM WhatsApp tab renders {{N}} filled (crm substitutes the display from template_parameters).
		if variables:
			self.template_parameters = frappe.as_json([v["value"] for v in variables])
		self._apply_send_result(adapter.send_template(account, self.to, template, variables))

	def notify(self, data):
		"""Backstop (guardrail #5): an adapter-backed account must never reach Meta's notify().

		Our builders send through the resolved adapter and never call this for an adapter-backed
		account. If some other code path does, fail loud rather than POST to Meta.
		"""
		if self._channel_account() is not None:
			frappe.throw(
				_("Blocked a Meta-bound send on WhatsApp account '{0}'. Sends go through the account's "
				  "own channel adapter.").format(self.whatsapp_account),
				title=_("Blocked send"),
			)
		return super().notify(data)

	def send_read_receipt(self):
		"""No-Meta backstop: upstream POSTs a read receipt to Meta's Graph API.

		No adapter on our contract exposes a read-receipt endpoint, so for an adapter-backed account
		this is a no-op — never fall through to super() (which would reach Meta).
		"""
		if self._channel_account() is not None:
			return None
		return super().send_read_receipt()

	# --- helpers ---
	def _body_parameters(self, template, adapter, account):
		"""Resolve template body placeholders into the [{name, value}] shape a send takes.

		Variables are matched by NAME, not by the {{N}} position shown in the body — sending positional
		"1","2" fills the slots blank. The real names come from the adapter, which is the one thing that
		knows how its provider names them. Empty for a static-body template.
		"""
		names = adapter.template_variables(account, template)

		def _name(idx):  # idx = the 1-based {{N}} slot
			return names[idx - 1] if 0 < idx <= len(names) else str(idx)

		# Primary path: explicit body_param keyed by the real {{N}} index. Send "" for an empty slot rather than dropping it (dropping would shift later slots).
		if self.body_param:
			try:
				bp = frappe.parse_json(self.body_param)
			except Exception:
				frappe.log_error(title="WATI: unreadable message body_param")
				return []
			return [
				{"name": _name(int(k)), "value": "" if bp[k] is None else str(bp[k])}
				for k in sorted(bp, key=lambda x: int(x))
			]
		# Fallback: positional values resolved from field_names (notification / automated path). field_names is ordered to match {{1}},{{2}},… by the operator.
		field_names = (template.field_names or "").split(",") if template.field_names else []
		if not field_names:
			return []
		# A Link field holds the target's primary key, and for a grain master that key is a composite `::` string. These values go to a patient's phone, so resolve them to the title first.
		if self.flags.get("custom_ref_doc"):
			cv = self.flags.custom_ref_doc
			values = [
				labels.shown(self.reference_doctype, fn.strip(), cv.get(fn.strip()))
				for fn in field_names
			]
		elif self.reference_doctype and self.reference_name:
			ref = frappe.get_doc(self.reference_doctype, self.reference_name)
			values = [
				labels.shown(self.reference_doctype, fn.strip(), ref.get_formatted(fn.strip()))
				for fn in field_names
			]
		else:
			return []
		return [
			{"name": _name(i + 1), "value": "" if v is None else str(v)}
			for i, v in enumerate(values)
		]

	def _apply_send_result(self, result):
		"""Apply a `SendResult` to the row. The adapter already classified it — the one source of truth,
		shared with every other send path — so this only applies this path's side-effect: throw on
		failure (which rolls back the insert), else stamp the correlation id and mark sent.

		An UNKNOWN outcome keeps the row and does not throw. The message may already be on the patient's
		phone, so rolling it back would erase the only record of a send that happened, and calling it
		failed is what makes a rep send a clinical message twice. The row carries no correlation id, and
		a backfill reconciles it against the provider's own history.
		"""
		if result.unknown:
			frappe.msgprint(
				_("WhatsApp gave no answer for this send. It may already have been delivered — check the "
				  "thread before sending it again."),
				title=_("Delivery unconfirmed"),
				indicator="orange",
			)
			return
		if not result.accepted:
			self.status = "failed"
			# The throw rolls back this insert, so audit the failure to Error Log out-of-band (defer_insert survives the rollback) — native capture, no hand-rolled audit row.
			frappe.log_error(
				title="WhatsApp manual send failed",
				message=result.error or "message could not be sent",
				reference_doctype=self.reference_doctype,
				reference_name=self.reference_name,
				defer_insert=True,
			)
			frappe.throw(
				_("WhatsApp send failed: {0}").format(result.error or _("message could not be sent")),
				title=_("WhatsApp Error"),
			)
		# ONLY the correlation id the provider's own status events echo. Storing any other id it happens to mint is how a message's delivered/read/failed silently never arrives.
		if result.correlation_id:
			self.message_id = result.correlation_id
		self.status = "sent"
