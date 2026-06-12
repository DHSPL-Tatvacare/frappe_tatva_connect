"""WATI override of frappe_whatsapp's `WhatsApp Notification` (Seam 2).

Automated / scheduled / DocType-event template sends go through
`WhatsApp Notification`, which builds its OWN Meta payload and calls its own
`notify()` — bypassing the `WhatsApp Message` override entirely. If we don't
override this too, every automated notification still hits Meta.

We override `notify()` and translate the Meta template payload it receives into
a WATI sendTemplateMessage (the template name + body params are all in the
payload), then insert the resulting `WhatsApp Message` row exactly as upstream
does so it threads + shows in the lead tab. No Meta call, ever.

Registered via `override_doctype_class` in hooks.py.
"""
import json

import frappe
from frappe import _
from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_notification.whatsapp_notification import (
	WhatsAppNotification,
)
from frappe_whatsapp.utils import get_whatsapp_account

from tatva_connect.whatsapp import api as wati


class WATINotification(WhatsAppNotification):
	def notify(self, data, doc_data=None):
		# Resolve the account exactly as upstream does.
		if self.whatsapp_account:
			account = frappe.get_doc("WhatsApp Account", self.whatsapp_account)
		else:
			account = get_whatsapp_account(account_type="outgoing")
		if not account:
			frappe.throw(_("Please set a default outgoing WhatsApp Account"))

		# No-Meta guarantee: only WATI may send; non-WATI raises (never Meta fallback).
		wati.assert_wati(account)
		wati.assert_enabled()

		tpl = data.get("template", {}) or {}
		params = self._wati_params_from_meta(tpl)
		success = False
		error_message = None
		try:
			resp = wati.send_template_message(
				account,
				to_number=wati.normalize_number(data.get("to")),
				template_name=tpl.get("name"),
				broadcast_name=f"crm_notif_{frappe.scrub(self.template or self.name)}",
				parameters=params,
			)
			# Same success contract as the manual-send path (one brain): classify once,
			# then this path's side-effect is to raise (caught below -> Notification Log).
			r = wati.classify_send_response(resp)
			if r.failed:
				raise Exception(r.reason or "WATI send failed")
			message_id = r.message_id

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
				"template_parameters": json.dumps([p["value"] for p in params], default=str) if params else None,
				"whatsapp_account": account.name,
			}
			if doc_data:
				new_doc.update({"reference_doctype": doc_data.doctype, "reference_name": doc_data.name})
			frappe.get_doc(new_doc).save(ignore_permissions=True)

			# Preserve upstream's set-property-after-alert behaviour.
			if doc_data and self.set_property_after_alert and self.property_value:
				meta = frappe.get_meta(doc_data.get("doctype"))
				df = meta.get_field(self.set_property_after_alert)
				if df:
					value = self.property_value
					if df.fieldtype in frappe.model.numeric_fieldtypes:
						value = frappe.utils.cint(value)
					frappe.db.set_value(doc_data.get("doctype"), doc_data.get("name"), self.set_property_after_alert, value)

			frappe.msgprint(_("WhatsApp Message Triggered (WATI)"), indicator="green", alert=True)
			success = True
		except Exception as e:
			error_message = str(e)
			frappe.msgprint(
				_("Failed to trigger WATI WhatsApp message: {0}").format(error_message),
				indicator="red",
				alert=True,
			)
		finally:
			meta = {"error": error_message} if not success else {"transport": "wati", "to": data.get("to")}
			frappe.get_doc(
				{"doctype": "WhatsApp Notification Log", "template": self.template, "meta_data": meta}
			).insert(ignore_permissions=True)

	def _wati_params_from_meta(self, tpl):
		"""Pull the body component's positional params out of the Meta payload."""
		for component in tpl.get("components") or []:
			if component.get("type") == "body":
				params = component.get("parameters") or []
				return [{"name": str(i + 1), "value": p.get("text")} for i, p in enumerate(params)]
		return []
