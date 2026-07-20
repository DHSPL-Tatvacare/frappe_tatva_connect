# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""The server read behind the eye toggle on the account forms: a saved Password field holds only asterisks."""
import frappe
from frappe import _
from frappe.rate_limiter import rate_limit

# Only account-config secrets — never a user credential, never an arbitrary Password field.
REVEALABLE = {
	"WhatsApp Account": {
		"token",
		"custom_webhook_token",
		"custom_webhook_token_previous",
		"custom_webhook_hmac_secret",
	},
	"CRM Telephony Account": {
		"api_token",
		"click_to_call_api_key",
		"webhook_token",
		"webhook_token_previous",
		"webhook_hmac_secret",
	},
	"CRM Push Settings": {"service_account_json", "web_api_key", "vapid_key"},
	"CRM Maps Settings": {"google_maps_api_key", "google_maps_browser_key"},
	"CRM Facebook Settings": {"app_secret"},
	"Lead Sync Source": {"access_token"},
	"Facebook Page": {"access_token"},
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

	# Written BEFORE the secret is read, so a failure below still leaves the attempt on record.
	_audit(doctype, name, fieldname)

	doc = frappe.get_doc(doctype, name)
	return {"value": doc.get_password(fieldname, raise_exception=False) or ""}


def _audit(doctype: str, name: str, fieldname: str) -> None:
	"""Frappe's own Access Log row, written durably: who read which secret and when is what justifies
	revealing these fields at all, so it is not allowed to be best-effort.

	Core's `make_access_log` is not used because outside a test it calls `deferred_insert()`, which pushes
	the row to Redis — no persistence, an LRU eviction policy, and `creation`/`owner` re-stamped at flush
	time rather than at the moment of the read. The row is inserted here and committed at once, so it
	survives whatever the rest of the request does; the reveal itself writes nothing else, so the commit
	carries this row alone."""
	frappe.get_doc({
		"doctype": "Access Log",
		"user": frappe.session.user,
		"export_from": doctype,
		"reference_document": name,
		"method": f"reveal:{fieldname}",
	}).insert(ignore_permissions=True)  # authz-ok: tier-a — audit row about the caller, written on their behalf
	frappe.db.commit()
