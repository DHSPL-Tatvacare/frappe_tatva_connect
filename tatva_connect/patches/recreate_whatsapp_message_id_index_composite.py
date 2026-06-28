"""Replace the single-column message_id unique index with composite (message_id, reference_name) so a shared number can mirror a message onto multiple leads while still per-lead deduping; skip+log if legacy dupes exist."""
import frappe


def execute():
	table = "tabWhatsApp Message"
	old_index = "message_id_unique"
	new_index = "message_id_reference_unique"

	# Already migrated?
	if frappe.db.has_index(table, new_index):
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
	if frappe.db.has_index(table, old_index):
		# sqli-ok: no frappe helper drops an index; table/old_index are code constants, no user input.
		frappe.db.sql(f"ALTER TABLE `{table}` DROP INDEX `{old_index}`")

	try:
		frappe.db.add_unique("WhatsApp Message", ["message_id", "reference_name"], new_index)
	except Exception:
		frappe.log_error(
			title="WATI: composite message_id index failed",
			message=frappe.get_traceback(),
		)
