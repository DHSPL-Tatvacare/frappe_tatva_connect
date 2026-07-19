"""The token-expiry alert as a native Notification — no job is written, because Frappe already has the
scheduler for "Days Before" on a Date field.

Ships DISABLED: the operator turns it on at go-live and names the recipients, like every other
automation here. Seeded so the alert is one click away rather than something a person must know to build.
"""
import frappe

NAME = "Facebook Token Expiring"
_DAYS_BEFORE = 7


def ensure_notification():
	"""Idempotent: creates the alert once, and never re-enables or re-addresses an operator's copy."""
	if frappe.db.exists("Notification", NAME):
		return
	if not frappe.get_meta("Lead Sync Source").has_field("token_expires_on"):
		return  # the fixture has not landed yet; a later migrate seeds this

	frappe.get_doc({
		"doctype": "Notification",
		"name": NAME,
		"subject": f"Facebook token expires in {_DAYS_BEFORE} days: {{{{ doc.name }}}}",
		"document_type": "Lead Sync Source",
		"event": "Days Before",
		"date_changed": "token_expires_on",
		"days_in_advance": _DAYS_BEFORE,
		"enabled": 0,
		"channel": "Email",
		"condition": "doc.enabled",
		"message": (
			"The Facebook Page token on {{ doc.name }} expires on {{ doc.token_expires_on }}.\n\n"
			"Once it lapses the crawl returns no leads and reports no error. "
			"Replace the token on the Lead Sync Source before that date."
		),
	}).insert(ignore_permissions=True)  # authz-ok: tier-a — migrate-time seed of a dormant operator alert
