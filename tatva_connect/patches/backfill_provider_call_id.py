"""Move the provider's call id out of the row name and into its own column.

Calls logged before `custom_provider_call_id` existed were named after the provider's call id, because
crm's autoname was `field:id` and its `id` field held that id. The naming series is changing, so every
telephony lookup now reads the column instead. Rows written under the old scheme carry nothing in it,
and a pull that cannot find them would log each one a second time.

Only rows a provider wrote are touched. A call a rep logged by hand keeps an empty key column, which is
precisely what makes it unreachable from the reconcile pull.
"""
import frappe

from tatva_connect.telephony import providers, writer

DOCTYPE = "CRM Call Log"


def execute():
	rows = frappe.get_all(
		DOCTYPE,
		filters={
			"telephony_medium": ["in", list(providers.PROVIDERS)],
			writer.CALL_KEY_FIELD: ["in", ("", None)],
		},
		fields=["name", "id"],
	)
	for row in rows:
		if not row.id:
			continue
		frappe.db.set_value(DOCTYPE, row.name, writer.CALL_KEY_FIELD, row.id, update_modified=False)
	frappe.db.commit()
