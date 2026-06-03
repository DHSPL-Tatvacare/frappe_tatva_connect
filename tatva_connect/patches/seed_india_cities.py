"""Seed the CRM City master from the bundled India city list (dr5hn, ODbL).

One-time bulk load so the City picker is populated. Idempotent — only inserts
names not already present. New cities also grow organically via the form's
"city isn't listed" manual path.
"""
import json
import os

import frappe


def execute():
	path = frappe.get_app_path("tatva_connect", "data", "india_cities.json")
	if not os.path.exists(path):
		return
	with open(path) as f:
		cities = json.load(f)
	existing = set(frappe.get_all("CRM City", pluck="name"))
	now = frappe.utils.now()
	rows = [[c, c, "Administrator", "Administrator", now, now] for c in cities if c not in existing]
	if rows:
		frappe.db.bulk_insert(
			"CRM City",
			fields=["name", "city_name", "owner", "modified_by", "creation", "modified"],
			values=rows,
			ignore_duplicates=True,
		)
		frappe.db.commit()
