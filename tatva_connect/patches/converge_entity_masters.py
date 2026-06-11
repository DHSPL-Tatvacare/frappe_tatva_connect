"""Converge existing dev rows to the new entity-master keying (M-1 / Doc B Phase 1).

Runs post_model_sync, AFTER the CRM Doctor/Hospital/City JSON (autoname hash /
format, unique dropped) is in place.

* CRM Doctor / CRM Hospital: were `field:<name>` (name == display text). Rename each
  old-style row to an opaque hash via rename_doc, which cascades the only inbound
  link (CRM Enrolment Submission has none today, but rename_doc is link-safe). Tiny
  tables (3 rows each on dev), so per-row rename is cheap and preserves the data.
* CRM City: was `field:city_name` (unique nationwide, name == city). Wipe + reseed
  state-aware (name == "<city>::<state>"). City carries no business links (it's a
  pick-list source on the lead's free-text custom_city), so a wipe+reseed is safe
  and far cheaper than ~4,080 renames.

Idempotent: a row already on the new key is skipped. Never deletes real lead data —
City wipe is a reference-set reseed, not business data (see plan §10).
"""
import frappe

from tatva_connect.taxonomy.normalize import normalize_display


def execute():
	_converge_named_master("CRM Doctor", "doctor_name")
	_converge_named_master("CRM Hospital", "hospital_name")
	_reseed_cities()


def _converge_named_master(doctype, display_field):
	"""Rename any row whose name still equals its display text to an opaque hash."""
	if not frappe.db.exists("DocType", doctype):
		return
	rows = frappe.get_all(doctype, fields=["name", display_field])
	for r in rows:
		display = r.get(display_field)
		# Old-style rows have name == display text. New (hash) rows won't match.
		if display and r.name == display:
			new_name = frappe.generate_hash(length=10)
			# rename_doc cascades inbound links; merge=False (these are distinct rows).
			frappe.rename_doc(doctype, r.name, new_name, force=True, show_alert=False)
	frappe.db.commit()


def _reseed_cities():
	"""Wipe old single-key City rows and reseed the composite (city::state) set."""
	if not frappe.db.exists("DocType", "CRM City"):
		return
	# A row is "old-style" if its name does not contain the composite separator.
	old = [n for n in frappe.get_all("CRM City", pluck="name") if "::" not in (n or "")]
	if old:
		# Direct delete of reference rows (no business links). Bulk for ~4,080 rows.
		frappe.db.delete("CRM City", {"name": ["in", old]})
		frappe.db.commit()
	# Reseed from the bundled dataset (idempotent; inserts composite keys).
	from tatva_connect.patches import seed_india_cities

	seed_india_cities.execute()
