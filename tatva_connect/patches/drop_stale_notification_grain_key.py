# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""`rename_field` COPIES: it left `grain_key` behind on any site that ran rename_notification_grain_to_event, now NULL. Left there, a re-run of that patch would copy those NULLs over `event_key` and wipe every rep's opt-ins, so the dead column goes."""
import frappe

from tatva_connect.patches import _schema

DOCTYPE = "CRM Notification Subscription"


def execute():
	# The rename patch drops grain_key with a raw ALTER, which does NOT invalidate frappe's
	# `table_columns::` cache (database.py:1334) — so has_column() below would read a stale list, claim the
	# column is still there, and DROP a column that is already gone (1091). Frappe busts this key itself
	# before reading columns after a schema change (model/meta.py:976).
	_schema.refresh(f"tab{DOCTYPE}")
	if not frappe.db.has_column(DOCTYPE, "grain_key"):
		return
	if not frappe.db.has_column(DOCTYPE, "event_key"):
		return  # the rename has not run here; that patch owns the move.
	_schema.ddl(f"ALTER TABLE `tab{DOCTYPE}` DROP COLUMN `grain_key`", f"tab{DOCTYPE}")
