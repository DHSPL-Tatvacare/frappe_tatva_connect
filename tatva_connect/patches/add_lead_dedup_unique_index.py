"""UNIQUE (mobile_no, custom_vertical, custom_group) on CRM Lead — the partner API's dedup rule,
enforced by the database rather than by an unguarded read-then-write.

`partner._upsert_one` looks the tuple up with a non-locking get_value and inserts on a miss. Nothing
serialised two concurrent requests: two `lead_create` calls for the same new patient land on two
workers, both miss, both insert, both commit. Thereafter `resolve_lead` — the shared brain behind
EVERY activity, file and call endpoint — returns an arbitrary one of the two, so the partner's
children scatter across both leads and later creates update only one. No API path heals it, and a
parallelised backfill (the documented use of `lead_create_bulk`) produces it reliably.

MariaDB does not constrain NULL tuples, so leads with no phone are left alone. That is correct: no
phone means no dedup key, and the API cannot create such a lead (`_upsert_one` throws without one).

Skip + log if duplicates already exist — the index cannot be created over them, and choosing which
row survives is a business decision, not a migration's. De-duplicate, then re-run. Same stance as
recreate_whatsapp_message_id_index_composite.
"""
import frappe

INDEX = "ix_lead_dedup_unique"
FIELDS = ["mobile_no", "custom_vertical", "custom_group"]


def execute():
	table = "tabCRM Lead"
	if frappe.db.has_index(table, INDEX):
		return

	# sqli-ok: table and column names are code constants; no user input reaches this statement.
	dupes = frappe.db.sql(
		f"""
		SELECT mobile_no, custom_vertical, custom_group, COUNT(*) AS n
		FROM `{table}`
		WHERE mobile_no IS NOT NULL AND mobile_no != ''
		GROUP BY mobile_no, custom_vertical, custom_group
		HAVING COUNT(*) > 1
		LIMIT 10
		""",
		as_dict=True,
	)
	if dupes:
		frappe.log_error(
			title="CRM Lead dedup unique index skipped",
			message=(
				"Duplicate (mobile_no, custom_vertical, custom_group) tuples exist on CRM Lead, so the "
				"unique index was NOT created and the partner API's dedup rule remains unenforced at "
				"the database. De-duplicate, then re-run this patch.\n\n"
				f"Sample (up to 10): {dupes}"
			),
		)
		return

	try:
		frappe.db.add_unique("CRM Lead", FIELDS, INDEX)
	except Exception:
		frappe.log_error(
			title="CRM Lead dedup unique index failed",
			message=frappe.get_traceback(),
		)
