"""Baseline the five email switches on frappe's per-user `Notification Settings` to OFF, once.

Frappe emails on every assignment (`assign_to._add` calls `notify_assignment()` and there is no notify=0
to pass) and all five email Checks ship at 1, so a rep is mailed for every lead handed to them.

A PATCH, because a baseline is set once and then belongs to the user. Patch Log records this and it never
runs again on the site, so a rep who turns email back on keeps that through every future deploy. A seed
would be wrong: manifests are re-run by design and would undo them. The `-default` Property Setters cover
rows created from here on, which is the same rule applied at the other end.

`enabled` is untouched — it is the master for the in-app bell, the SPA tray and push as well.
"""
import frappe

FIELDS = (
	"enable_email_notifications",
	"enable_email_assignment",
	"enable_email_mention",
	"enable_email_share",
	"enable_email_event_reminders",
)


def execute():
	if not frappe.db.table_exists("Notification Settings"):
		return
	settings = frappe.qb.DocType("Notification Settings")
	query = frappe.qb.update(settings)
	for field in FIELDS:
		query = query.set(settings[field], 0)
	query.run()
	# The per-user notification config is cached, and a bulk update runs no on_update to clear it.
	frappe.clear_cache()
