# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""`grain` on a notification subscription meant a notifiable moment, not the business grain (product line / group / program) it reads as — one word, two meanings, on forms an operator reads side by side. The column is now `event_key`; the stored keys are unchanged."""
import frappe

from tatva_connect.patches import _schema

DOCTYPE = "CRM Notification Subscription"


def execute():
	# Read reality, not the cache: an earlier patch in the same migrate changed this table, and frappe's cached column list would answer from before that. Its own successor already does this.
	_schema.refresh(f"tab{DOCTYPE}")
	if not frappe.db.has_column(DOCTYPE, "grain_key"):
		return
	frappe.reload_doc("notifications", "doctype", "crm_notification_subscription")
	_schema.rename_field(DOCTYPE, "grain_key", "event_key")
	# rename_field COPIES: it leaves grain_key behind, now NULL. Left there, a second run would copy those
	# NULLs over event_key and wipe every rep's opt-ins — so the old column goes, and the guard above can bite.
	_schema.ddl(f"ALTER TABLE `tab{DOCTYPE}` DROP COLUMN `grain_key`", f"tab{DOCTYPE}")
