"""Shared admin helper: the exact, ready-to-register inbound webhook URL(s) for an
account form — for BOTH providers (WATI WhatsApp, Acefone telephony).

The per-account webhook token is a Password field, so once saved its form value is the
`***` placeholder — the Desk client scripts CANNOT build the URL from `frm.doc.<token>`.
This module is the one place that reads the real secret server-side via `get_password`
and hands the assembled URL(s) back, so the form's banner + Copy button always show the
true URL. System Manager only (default `@frappe.whitelist` gating).

URL shapes (nginx rewrites the pretty trailing segment to `?token=`, see
nginx/frappe.conf.template):
  * WhatsApp Account      -> /webhooks/whatsapp/wati/<token>            (one URL)
  * CRM Telephony Account -> /webhooks/telephony/<provider>/<token>/<event>  (one per event)
"""
import frappe
from frappe import _
from frappe.utils import get_url

from tatva_connect.webhooks import registry


@frappe.whitelist()
def get_account_webhook_urls(account_doctype, name):
	"""Return the real inbound webhook URL(s) for one account, reading the token via
	`get_password` (the Password column holds `***` once saved). System Manager only.

	Returns `{"token_set": bool, "urls": [str, ...]}`: `token_set` is False (urls empty)
	when the account has no token yet — the form shows a "generate a token" prompt instead
	of a half-built URL."""
	cfg = registry.by_account_doctype(account_doctype)
	if not cfg:
		frappe.throw(_("Unsupported account doctype: {0}").format(account_doctype))

	doc = frappe.get_cached_doc(account_doctype, name)
	token = doc.get_password(cfg["token_field"], raise_exception=False)
	if not token:
		return {"token_set": False, "urls": []}
	return {"token_set": True, "urls": cfg["build_urls"](get_url().rstrip("/"), doc, token)}
