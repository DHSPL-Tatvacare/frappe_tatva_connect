"""Add a unique index on WhatsApp Message.message_id to close the redelivered-webhook double-insert race (NULLs unaffected); skip+log if legacy dupes exist."""
import frappe


def execute():
	table = "tabWhatsApp Message"
	index_name = "message_id_unique"

	existing = frappe.db.sql(
		f"SHOW INDEX FROM `{table}` WHERE Key_name = %s", index_name
	)
	if existing:
		return

	dupes = frappe.db.sql(
		f"""
		SELECT message_id FROM `{table}`
		WHERE message_id IS NOT NULL AND message_id != ''
		GROUP BY message_id HAVING COUNT(*) > 1 LIMIT 1
		"""
	)
	if dupes:
		frappe.log_error(
			title="WATI: message_id unique index skipped",
			message=(
				"Duplicate message_id values exist on WhatsApp Message; the unique "
				"index was not created. De-duplicate, then re-run this patch."
			),
		)
		return

	try:
		frappe.db.sql(
			f"ALTER TABLE `{table}` ADD UNIQUE INDEX `{index_name}` (`message_id`)"
		)
	except Exception:
		frappe.log_error(
			title="WATI: message_id unique index failed",
			message=frappe.get_traceback(),
		)
