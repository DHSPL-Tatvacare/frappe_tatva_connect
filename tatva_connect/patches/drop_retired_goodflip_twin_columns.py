"""Drop the four Goodflip columns whose LeadSquared source has been dead for the whole export.

Each is the empty half of a TWIN: two columns for one fact, one fed by a field LSQ stopped writing and one
fed by the field it still writes. Their catalogue rows and grain ticks went on 2026-08-19 through
`2026-08-02-retire-superseded-catalog-keys`, so nothing in the product can address them any more — the
Data tab, the partner API, an activity form and a Smart View all read that one registry. The columns
themselves survive, because frappe never drops a column when a field leaves a doctype, and a column that
answers nothing still reads as a live field to anyone who meets it in raw SQL. That is exactly how the
2026-08-14 Anaya seed came to write `can_set` believing it granted something.

The dead source against the live one, counted across ALL THREE pulls — Goodflip 18,063 + Anaya 16,176 +
TatvaPractice 29,417 leads, because a column is shared and one account's dead field is another's live one:
    custom_city_dropdown   mx_City_Dropdown  0   vs  mx_City              12,033
    custom_bca             mx_BCA            0   vs  mx_BCA_Device           676
    custom_cgm_sold        mx_CGM_Sold       0   vs  mx_Plan_includes_CGM    672
    custom_nutra_given     mx_Nutra_Given    0   vs  mx_Nutraceuticals_Sold  673
`custom_labtest_booked` was in this list and is NOT dropped: mx_Labtest_Booked is empty for Goodflip but
carries 10 Anaya leads and is a live row in that account's mapping.

The DECLARATION goes with the column or the next migrate rebuilds it: the Custom Field rows are deleted
here, because removing them from `fixtures/custom_field.json` only stops them shipping — it never deletes
a row a site already holds.

DDL through `_schema.ddl` (patches.txt rule 1) — a raw ALTER leaves frappe's cached column list stale and
the next `has_column` in the same migrate reads a pre-DDL lie. Idempotent (has_column guard). No
schema_setup twin: a fresh site's fixtures no longer declare these fields, so there is nothing to remove.
"""

import frappe

from tatva_connect.patches import _schema

_TWINS = (
	("CRM Lead", "custom_city_dropdown"),
	("CRM Plan Profile", "custom_bca"),
	("CRM Plan Profile", "custom_cgm_sold"),
	("CRM Plan Profile", "custom_nutra_given"),
)


def execute():
	for doctype, fieldname in _TWINS:
		cf = f"{doctype}-{fieldname}"
		if frappe.db.exists("Custom Field", cf):
			frappe.delete_doc("Custom Field", cf, force=True, ignore_permissions=True)  # authz-ok: tier-a — migration, runs as Administrator at migrate
		table = f"tab{doctype}"
		if frappe.db.has_column(doctype, fieldname):
			_schema.ddl(f"ALTER TABLE `{table}` DROP COLUMN `{fieldname}`", table)
		frappe.clear_cache(doctype=doctype)
	frappe.db.commit()
