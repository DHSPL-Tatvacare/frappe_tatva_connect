# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""The ONE door for raw DDL in a patch. Frappe caches a table's column list (`table_columns::tab...`, database.py:1334) and a raw ALTER never invalidates it, so the next `has_column()` in the same migrate reads a pre-DDL lie: that is what killed the UAT deploy — a patch dropped a column another patch had already dropped (1091). Frappe busts this key itself before reading columns after a schema change (model/meta.py:976); every patch does its DDL through here so the guard that follows sees reality."""
import frappe


def refresh(table: str):
	"""Forget the cached column list for a table whose schema we just changed. `table` carries its tab prefix."""
	frappe.client_cache.delete_value(f"table_columns::{table}")


def ddl(sql: str, table: str):
	"""Run a raw DDL statement and immediately un-cache the table's columns."""
	frappe.db.sql_ddl(sql)
	refresh(table)


def rename_column(doctype: str, old: str, new: str):
	"""frappe.db.rename_column ALTERs the table and leaves the cached column list stale — go through here."""
	frappe.db.rename_column(doctype, old, new)
	refresh(f"tab{doctype}")


def rename_field(doctype: str, old: str, new: str):
	"""frappe.model.utils.rename_field ALTERs the table too, and copies: same staleness, same door."""
	from frappe.model.utils.rename_field import rename_field as _rename_field

	_rename_field(doctype, old, new)
	refresh(f"tab{doctype}")
