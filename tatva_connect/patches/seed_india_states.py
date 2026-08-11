"""Seed CRM State from the SAME bundled india_cities.json the city seed reads, so the two masters can never disagree on a spelling; idempotent, inserts only missing rows."""
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
	existing = set(frappe.get_all("CRM State", pluck="name"))
	# The state column of the city list IS the vocabulary — never a second hand-typed list.
	states = sorted({(state or "").strip() for _city, state in rows if (state or "").strip()})
	to_insert = [
		[s, s, "Administrator", "Administrator", now, now] for s in states if s not in existing
	]
	if to_insert:
		frappe.db.bulk_insert(
			"CRM State",
			fields=["name", "state_name", "owner", "modified_by", "creation", "modified"],
			values=to_insert,
			ignore_duplicates=True,
		)
	frappe.db.commit()
