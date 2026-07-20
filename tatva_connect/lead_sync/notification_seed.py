"""The two Facebook alerts as native Notifications — no job is written, because Frappe already schedules
"Days Before" and "Days After" on a date field.

  * ACCESS EXPIRY — two clocks can end a crawl and `token_expires_on` holds whichever falls first: the
    token's own expiry, or the data-access window that lapses about 90 days after the person last engaged
    with the app. The second binds even a token Meta issued without an expiry, which is why the alert is
    named for access rather than for the token.
  * SILENCE — `last_synced_at` only moves when a lead actually lands, so it IS the date of the last lead.
    A crawl that has stopped returning anything looks identical to a quiet week: Graph answers `{"data":
    []}` with HTTP 200 and nothing errors. This is the alert that tells the difference.

Both ship DISABLED: the operator turns them on at go-live and names the recipients, like every other
automation here. Seeded so each is one click away rather than something a person must know to build.
"""
import frappe

DOCTYPE = "Lead Sync Source"

_ALERTS = (
	{
		"name": "Facebook Access Expiry",
		"event": "Days Before",
		"date_changed": "token_expires_on",
		"days_in_advance": 7,
		"subject": "Facebook access expires in 7 days: {{ doc.name }}",
		"message": (
			"Facebook stops returning leads for {{ doc.name }} on {{ doc.token_expires_on }}.\n\n"
			"This is whichever comes first: the token's own expiry, or the end of Facebook's data-access "
			"window, which lapses about 90 days after the person last engaged with the app and applies "
			"even to a token that never expires.\n\n"
			"Nothing errors when it happens. The crawl simply returns nothing. "
			"Generate a fresh token and paste it on the Lead Sync Source before that date."
		),
	},
	{
		"name": "Facebook Leads Stopped",
		"event": "Days After",
		"date_changed": "last_synced_at",
		"days_in_advance": 3,
		"subject": "No Facebook leads for 3 days: {{ doc.name }}",
		"message": (
			"{{ doc.name }} has not received a lead since {{ doc.last_synced_at }}.\n\n"
			"A crawl that has stopped looks exactly like a quiet week: when access lapses, when the form "
			"is duplicated and submissions move to the new one, or when the token is revoked, Facebook "
			"answers with an empty list and HTTP 200. Nothing errors, and the source keeps reporting "
			"healthy.\n\n"
			"Check the Failed Lead Sync Log for this source, then confirm the form is still the one "
			"marketing is running."
		),
	},
)


def ensure_notification():
	"""Declare the end state on every migrate: one dormant alert per row above, correctly worded.

	Runs from after_migrate, so this is where the alerts are asserted rather than patched. A dormant row
	is reworded to match; an ENABLED row is left entirely alone, because by then both the text and the
	recipients are the operator's. Never re-enables, never re-addresses."""
	meta = frappe.get_meta(DOCTYPE)
	for alert in _ALERTS:
		if not meta.has_field(alert["date_changed"]):
			continue  # the fixture has not landed yet; a later migrate seeds this one
		if frappe.db.exists("Notification", alert["name"]):
			_reword_while_dormant(alert)
			continue
		frappe.get_doc({
			"doctype": "Notification",
			"document_type": DOCTYPE,
			"channel": "Email",
			"condition": "doc.enabled",
			"enabled": 0,
			**alert,
		}).insert(ignore_permissions=True)  # authz-ok: tier-a — migrate-time seed of a dormant operator alert


def _reword_while_dormant(alert):
	"""Keep the wording current until an operator takes ownership by enabling it."""
	doc = frappe.get_doc("Notification", alert["name"])
	if doc.enabled or (doc.subject == alert["subject"] and doc.message == alert["message"]):
		return
	doc.subject = alert["subject"]
	doc.message = alert["message"]
	doc.save(ignore_permissions=True)  # authz-ok: tier-a — migrate-time reword of a dormant seeded alert
