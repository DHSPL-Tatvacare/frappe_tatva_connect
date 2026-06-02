"""Mirror WATI templates into the read-only `WhatsApp Templates` catalog.

The CRM picker reads the `WhatsApp Templates` doctype. WATI templates already
exist and are approved on WATI; we never create or edit them from Frappe. This
pulls `getMessageTemplates` and writes local rows as a **read-only reflection**,
using `db_insert` / `db_update` to bypass frappe_whatsapp's Meta-bound
validate/after_insert entirely. No Meta, no WATI push.
"""
import frappe

from tatva_connect.wati import api as wati


@frappe.whitelist()
def sync_from_wati(account_name=None):
	"""Reflect approved WATI templates into WhatsApp Templates. Returns counts."""
	if not account_name:
		account_name = frappe.db.get_value(
			"WhatsApp Account", {"is_default_outgoing": 1, "custom_is_wati": 1}, "name"
		) or frappe.db.get_value("WhatsApp Account", {"custom_is_wati": 1}, "name")
	if not account_name:
		frappe.throw("No WATI WhatsApp Account found to sync templates from.")

	account = frappe.get_doc("WhatsApp Account", account_name)
	wati.assert_wati(account)

	resp = wati.get_message_templates(account)
	items = resp.get("messageTemplates") or resp.get("templates") or resp.get("data") or []

	created = updated = 0
	for t in items:
		if (t.get("status") or "").upper() != "APPROVED":
			continue
		name = t.get("elementName")
		if not name:
			continue
		values = {
			"template_name": name,
			"actual_name": name,
			"language_code": ((t.get("language") or {}).get("value") or "en").replace("-", "_"),
			"status": "APPROVED",
			"category": t.get("category") or "UTILITY",
			"template": t.get("body") or "",
			"whatsapp_account": account_name,
		}
		if frappe.db.exists("WhatsApp Templates", name):
			doc = frappe.get_doc("WhatsApp Templates", name)
			doc.update(values)
			doc.db_update()  # bypass Meta-bound validate/update_template
			updated += 1
		else:
			doc = frappe.new_doc("WhatsApp Templates")
			doc.update(values)
			doc.name = name
			doc.db_insert()  # bypass Meta-bound after_insert
			created += 1

	frappe.db.commit()
	return {"created": created, "updated": updated, "approved_total": created + updated}
