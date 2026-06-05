"""Recreate WhatsApp Message uniqueness as composite (message_id, reference_name).

The original single-column unique index on `message_id`
(add_whatsapp_message_id_unique_index) blocked a shared WhatsApp number from
mirroring the same message onto more than one lead (the same patient enrolled in
two programs). Refresh on the second lead hit `DuplicateEntryError`, and an inbound
reply could land on only one lead. We replace it with a composite unique index so
each lead holds its own copy of a shared thread, while still deduping redelivered
webhooks per lead.

MariaDB treats NULLs as distinct in a unique index, so draft rows (no message_id)
are unaffected. Defensive: if legacy duplicate (message_id, reference_name) pairs
exist the composite index can't be created — log and skip rather than abort migrate.
"""
import frappe


def execute():
	table = "tabWhatsApp Message"
	old_index = "message_id_unique"
	new_index = "message_id_reference_unique"

	# Already migrated?
	if frappe.db.sql(f"SHOW INDEX FROM `{table}` WHERE Key_name = %s", new_index):
		return

	dupes = frappe.db.sql(
		f"""
		SELECT message_id, reference_name FROM `{table}`
		WHERE message_id IS NOT NULL AND message_id != ''
		GROUP BY message_id, reference_name HAVING COUNT(*) > 1 LIMIT 1
		"""
	)
	if dupes:
		frappe.log_error(
			title="WATI: composite message_id index skipped",
			message=(
				"Duplicate (message_id, reference_name) pairs exist on WhatsApp Message; "
				"the composite unique index was not created. De-duplicate, then re-run."
			),
		)
		return

	# Drop the old single-column index if present.
	if frappe.db.sql(f"SHOW INDEX FROM `{table}` WHERE Key_name = %s", old_index):
		frappe.db.sql(f"ALTER TABLE `{table}` DROP INDEX `{old_index}`")

	try:
		frappe.db.sql(
			f"ALTER TABLE `{table}` ADD UNIQUE INDEX `{new_index}` (`message_id`, `reference_name`)"
		)
	except Exception:
		frappe.log_error(
			title="WATI: composite message_id index failed",
			message=frappe.get_traceback(),
		)
