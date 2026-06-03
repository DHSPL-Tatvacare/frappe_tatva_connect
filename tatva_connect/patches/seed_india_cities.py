"""Seed the CRM City master from the bundled India city+state list (dr5hn, ODbL).

Each entry is [city_name, state]. State matches the form's State options exactly, so
the cascade (pick State -> search its cities) works. Idempotent: refreshes state on
existing rows and inserts new ones.
"""
import json
import os

import frappe


def execute():
	path = frappe.get_app_path("tatva_connect", "data", "india_cities.json")
	if not os.path.exists(path):
		return
	with open(path) as f:
		rows = json.load(f)
	now = frappe.utils.now()
	existing = {c.name: c.state for c in frappe.get_all("CRM City", fields=["name", "state"])}
	to_insert = []
	for city, state in rows:
		if city in existing:
			if existing[city] != state:
				frappe.db.set_value("CRM City", city, "state", state, update_modified=False)
		else:
			to_insert.append([city, city, state, "Administrator", "Administrator", now, now])
	if to_insert:
		frappe.db.bulk_insert(
			"CRM City",
			fields=["name", "city_name", "state", "owner", "modified_by", "creation", "modified"],
			values=to_insert,
			ignore_duplicates=True,
		)
	frappe.db.commit()
