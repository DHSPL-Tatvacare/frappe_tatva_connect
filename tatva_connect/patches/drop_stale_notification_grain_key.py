# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""`rename_field` COPIES: it left `grain_key` behind on any site that ran rename_notification_grain_to_event, now NULL. Left there, a re-run of that patch would copy those NULLs over `event_key` and wipe every rep's opt-ins, so the dead column goes."""
import frappe

DOCTYPE = "CRM Notification Subscription"


def execute():
	if not frappe.db.has_column(DOCTYPE, "grain_key"):
		return
	if not frappe.db.has_column(DOCTYPE, "event_key"):
		return  # the rename has not run here; that patch owns the move.
	frappe.db.sql_ddl(f"ALTER TABLE `tab{DOCTYPE}` DROP COLUMN `grain_key`")
