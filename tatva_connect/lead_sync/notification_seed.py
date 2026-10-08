"""The two Facebook alerts as native Notifications — no job is written, because Frappe already schedules
"Days Before" and "Days After" on a date field.

  * ACCESS EXPIRY — on the CRM Facebook App, which holds the one credential. Two clocks can end it and
    `token_expires_on` holds whichever falls first: the token's own expiry, or the data-access window that
    lapses about 90 days after the person last engaged with the app. A system user token has neither, so
    its date stays blank and this never fires.
  * SILENCE — `last_synced_at` only moves when a lead actually lands, so it IS the date of the last lead.
    A crawl that has stopped returning anything looks identical to a quiet week: Graph answers `{"data":
    []}` with HTTP 200 and nothing errors. This is the alert that tells the difference.

Both ship DISABLED: the operator turns them on at go-live and names the recipients, like every other
automation here. Seeded so each is one click away rather than something a person must know to build.
"""
import frappe

_ALERTS = (
	{
		"name": "Facebook Access Expiry",
		"document_type": "CRM Facebook App",
		"condition": "",
		"event": "Days Before",
		"date_changed": "token_expires_on",
		"days_in_advance": 7,
		"subject": "Facebook access expires in 7 days: {{ doc.app_name }}",
		"message": (
			"The token on Facebook App {{ doc.app_name }} stops working on {{ doc.token_expires_on }}.\n\n"
			"This is whichever comes first: the token's own expiry, or the end of Facebook's data-access "
			"window, which lapses about 90 days after the person last engaged with the app.\n\n"
			"Nothing errors when it happens. The crawl simply returns nothing. Paste a fresh token on "
			"the Facebook App and run Refresh From Facebook before that date, or replace it with a system "
			"user token, which never expires."
		),
	},
	{
		"name": "Facebook Leads Stopped",
		"document_type": "Lead Sync Source",
		"condition": "doc.enabled",
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

	Runs from after_migrate, so this is where the alerts are asserted rather than patched. Every row is kept
	pointed at the record and field it watches, because an alert reading a dropped field never fires and never
	says so. Only a dormant row is reworded: once enabled, the text and recipients are the operator's. Never
	re-enables, never re-addresses."""
	for alert in _ALERTS:
		if not frappe.get_meta(alert["document_type"]).has_field(alert["date_changed"]):
			continue  # the field has not landed yet; a later migrate seeds this one
		if frappe.db.exists("Notification", alert["name"]):
			_keep_current(alert)
			continue
		frappe.get_doc({
			"doctype": "Notification",
			"channel": "Email",
			"enabled": 0,
			**alert,
		}).insert(ignore_permissions=True)  # authz-ok: tier-a — migrate-time seed of a dormant operator alert


def _keep_current(alert):
	"""Point the alert at the record and field it watches, even once enabled; reword it only while dormant."""
	doc = frappe.get_doc("Notification", alert["name"])
	target = {k: alert[k] for k in ("document_type", "date_changed", "condition")}
	if not doc.enabled:
		target.update(subject=alert["subject"], message=alert["message"])
	if all((doc.get(k) or "") == v for k, v in target.items()):
		return
	doc.update(target)
	doc.save(ignore_permissions=True)  # authz-ok: tier-a — migrate-time upkeep of a seeded alert
