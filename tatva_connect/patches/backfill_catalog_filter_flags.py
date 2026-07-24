# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Land the filterable/sortable baseline on catalog rows created before it existed.

lead_sync.catalog_seed.ensure_rows runs on after_migrate and creates these rows; until now it wrote only
field_key/label/section/fieldname, so filterable and sortable fell to the column default of 0. The db-seeds
that re-declare them filterable are INSERT IGNORE, and the row already exists by then, so they no-op — the
flags never landed. Consequence: UTM Source/Campaign, both Facebook ids and Source Origin cannot be filtered
or sorted in a Smart View (smartview/api.py fails a filter on them closed, and ignores a sort).

catalog_seed now carries the baseline at creation, which fixes every future site. This patch is the one-shot
repair for a site whose rows already exist, and it runs exactly once.

Guarded on the untouched default: a row is updated only while it still reads filterable=0 AND sortable=0, so
an operator who deliberately cleared a flag is never overwritten. Idempotent; assumes nothing about what ran
before.
"""
import frappe

# The rows catalog_seed creates that the Smart View is expected to filter and sort on.
_KEYS = (
	"lead:facebook_lead_id",
	"lead:facebook_form_id",
	"lead:custom_source_origin",
	"acq:utm_source",
	"acq:utm_campaign",
)


def execute():
	for field_key in _KEYS:
		row = frappe.db.get_value(
			"CRM Lead API Field", field_key, ["filterable", "sortable"], as_dict=True
		)
		if not row or row.filterable or row.sortable:
			continue  # absent, or an operator already set one — leave it alone
		frappe.db.set_value(
			"CRM Lead API Field", field_key, {"filterable": 1, "sortable": 1}, update_modified=False
		)  # authz-ok: tier-c — migrate/patch, no session user
	frappe.db.commit()
