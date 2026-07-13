# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""The server read behind the eye toggle on the account forms: a saved Password field holds only asterisks."""
import frappe
from frappe import _
from frappe.rate_limiter import rate_limit

# Only account-config secrets — never a user credential, never an arbitrary Password field.
REVEALABLE = {
	"WhatsApp Account": {"token", "custom_webhook_token", "custom_webhook_token_previous"},
	"CRM Telephony Account": {
		"api_token",
		"click_to_call_api_key",
		"webhook_token",
		"webhook_token_previous",
	},
	"CRM Push Settings": {"service_account_json", "web_api_key", "vapid_key"},
}


# POST-only, because a GET puts the secret in the URL and so in history and every access log. The cap is per caller IP (frappe.rate_limit), so it slows a scripted sweep from one address — it does not bound a stolen session, and core's own frappe.client.get_password already exposes any Password field to a System Manager. The allowlist here is ergonomics and blast-radius, not a boundary.
@frappe.whitelist(methods=["POST"])
@rate_limit(limit=10, seconds=60)
def reveal(doctype: str, name: str, fieldname: str) -> dict:
	"""Return the plaintext of one Password field. System Manager + write permission only."""
	frappe.only_for("System Manager")
	if fieldname not in REVEALABLE.get(doctype, ()):
		frappe.throw(_("{0}.{1} is not a revealable secret.").format(doctype, fieldname))
	frappe.has_permission(doctype, "write", doc=name, throw=True)

	if frappe.get_meta(doctype).get_field(fieldname).fieldtype != "Password":
		frappe.throw(_("{0}.{1} is not a Password field.").format(doctype, fieldname))

	doc = frappe.get_doc(doctype, name)
	return {"value": doc.get_password(fieldname, raise_exception=False) or ""}
