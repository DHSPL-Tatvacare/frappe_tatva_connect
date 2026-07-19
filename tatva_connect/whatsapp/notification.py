"""Channel override of frappe_whatsapp's `WhatsApp Notification` (Seam 2).

Automated / scheduled / DocType-event template sends go through `WhatsApp Notification`, which builds
its OWN Meta payload and calls its own `notify()` — bypassing the `WhatsApp Message` override
entirely. If we don't override this too, every automated notification still hits Meta.

We override `notify()` and translate the Meta template payload it receives into a send through the
account's own channel adapter (the template name and body params are all in the payload), then insert
the resulting `WhatsApp Message` row exactly as upstream does so it threads and shows in the lead tab.
No Meta call, ever.

Registered via `override_doctype_class` in hooks.py.
"""
import frappe
from frappe import _
from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_notification.whatsapp_notification import (
	WhatsAppNotification,
)
from frappe_whatsapp.utils import get_whatsapp_account

from tatva_connect.channels import resolve
from tatva_connect.whatsapp import channel


class ChannelWhatsAppNotification(WhatsAppNotification):
	def notify(self, data, doc_data=None):
		# Resolve the account exactly as upstream does.
		if self.whatsapp_account:
			account = frappe.get_doc("WhatsApp Account", self.whatsapp_account)
		else:
			account = get_whatsapp_account(account_type="outgoing")
		if not account:
			frappe.throw(_("Please set a default outgoing WhatsApp Account"))

		# No-Meta guarantee: resolve the account's adapter. An account with no registered adapter (e.g. a Meta account) raises here — never a Meta fallback.
		channel.assert_enabled()
		adapter = resolve.adapter_for(account)

		tpl = data.get("template", {}) or {}
		params = self._variables_from_meta(tpl, adapter, account)
		success = False
		error_message = None
		try:
			# Same success contract as the manual-send path (one brain): the adapter classifies. Only a genuine refusal raises — an unknown outcome may already be on the patient's phone.
			result = adapter.send_template(
				account,
				data.get("to"),
				self.template,
				params,
				broadcast_name=f"crm_notif_{frappe.scrub(self.template or self.name)}",
			)
			if not (result.accepted or result.unknown):
				frappe.throw(
					_("WhatsApp send failed: {0}").format(result.error)
					if result.error
					else _("WhatsApp send failed")
				)
		except Exception as e:
			frappe.log_error(title="WhatsApp notification send failed")
			error_message = str(e)
			frappe.msgprint(
				_("Failed to trigger WhatsApp message: {0}").format(error_message),
				indicator="red",
				alert=True,
			)
		else:
			# The message has left the building. NOTHING below may report failure: an operator reading "failed" for a message the patient already received re-triggers it and sends it twice.
			try:
				message_id = result.correlation_id

				if not self.get("content_type"):
					self.content_type = "text"
				new_doc = {
					"doctype": "WhatsApp Message",
					"type": "Outgoing",
					"message": str(tpl),
					"to": data.get("to"),
					"message_type": "Template",
					"message_id": message_id,
					"content_type": self.content_type,
					"use_template": 1,
					"template": self.template,
					"template_parameters": frappe.as_json([p["value"] for p in params]) if params else None,
					"whatsapp_account": account.name,
				}
				if doc_data:
					new_doc.update({"reference_doctype": doc_data.doctype, "reference_name": doc_data.name})
				frappe.get_doc(new_doc).save(ignore_permissions=True)  # authz-ok: tier-b — outbound send, gated by the account's own grain check

				# Preserve upstream's set-property-after-alert behaviour.
				if doc_data and self.set_property_after_alert and self.property_value:
					meta = frappe.get_meta(doc_data.get("doctype"))
					df = meta.get_field(self.set_property_after_alert)
					if df:
						value = self.property_value
						if df.fieldtype in frappe.model.numeric_fieldtypes:
							value = frappe.utils.cint(value)
						frappe.db.set_value(doc_data.get("doctype"), doc_data.get("name"), self.set_property_after_alert, value)
			except Exception:
				# The send stands. This is a bookkeeping failure and it is logged as one.
				frappe.log_error(title="WhatsApp notification sent but not recorded")

			frappe.msgprint(_("WhatsApp Message Triggered"), indicator="green", alert=True)
			success = True
		finally:
			meta = (
				{"error": error_message}
				if not success
				else {"channel": adapter.DECLARATION.channel, "provider": adapter.DECLARATION.provider, "to": data.get("to")}
			)
			frappe.get_doc(
				{"doctype": "WhatsApp Notification Log", "template": self.template, "meta_data": meta}
			).insert(ignore_permissions=True)  # authz-ok: tier-b — outbound send, gated by the account's own grain check

	def _variables_from_meta(self, tpl, adapter, account):
		"""Body variables as [{name, value}] — names from the adapter (one brain with the manual path:
		adapter.template_variables), values from the operator's field mapping (the native `fields`
		table). Positional fallback when the template has no names."""
		names = adapter.template_variables(account, self.template) if self.template else []
		for component in tpl.get("components") or []:
			if component.get("type") == "body":
				params = component.get("parameters") or []
				return [
					{"name": names[i] if i < len(names) else str(i + 1), "value": p.get("text")}
					for i, p in enumerate(params)
				]
		return []
