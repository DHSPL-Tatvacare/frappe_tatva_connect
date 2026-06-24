"""Seed CRM City from the bundled India city+state list (dr5hn, ODbL), keyed "<city>::<state>" so a town name can recur across states; idempotent, inserts only missing composite rows."""
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
	# Existing composite keys ("<city>::<state>") already present.
	existing = set(frappe.get_all("CRM City", pluck="name"))
	to_insert = []
	seen = set()
	for city, state in rows:
		key = f"{city}::{state}"
		if key in existing or key in seen:
			continue
		seen.add(key)
		to_insert.append([key, city, state, "Administrator", "Administrator", now, now])
	if to_insert:
		frappe.db.bulk_insert(
			"CRM City",
			fields=["name", "city_name", "state", "owner", "modified_by", "creation", "modified"],
			values=to_insert,
			ignore_duplicates=True,
		)
	frappe.db.commit()
