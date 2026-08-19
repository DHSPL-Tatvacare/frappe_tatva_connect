"""Clear the free text left in columns that BECAME `Link -> CRM Picklist Value`.

A field that takes a controlled vocabulary is a Link into `CRM Picklist Value`, and a multi-value one must
be — `lead.detail._link_query` scopes a picker only for that exact fieldtype, so a `Data` or `Select`
column with `is_multi_value` ticked draws an UNSCOPED picker over every category's values. Two columns were
retyped for that reason and both still held what the old type wrote:

    CRM Lead.custom_condition                 a `Select` whose options were the raw distinct lead values,
                                              so it holds whole combinations ('t2d, htn, dyslipid')
    CRM Plan Profile.custom_nutraceuticals     a `Data` holding the LSQ token verbatim

A composite picklist PK always carries `::`; anything else is what the old type wrote, and left in place it
is a Link pointing at a row that does not exist — which the Desk renders and a report joins on.

Not a loss: both are MULTI-VALUE now, so their answers live as `CRM Lead Multi Value` rows and the Goodflip
re-migration reloads them from the same LeadSquared fields these columns were filled from. Cleared rather
than parsed — the loader's `_multi_value_pks` is the ONE split-and-resolve rule and is not copied here.

SUPERSEDES `blank_condition_free_text`, which did only the first column. One defect, one cure: the next
field retyped for the same reason is a line in `_COLUMNS`, not another patch.

Idempotent (only touches a value with no `::` in it). No DDL, so no schema_setup twin.
"""

import frappe

# (doctype, column) — every column retyped to `Link -> CRM Picklist Value` that predates the retype.
_COLUMNS = (
	("CRM Lead", "custom_condition"),
	("CRM Plan Profile", "custom_nutraceuticals"),
)


def execute():
	for doctype, fieldname in _COLUMNS:
		if not frappe.db.has_column(doctype, fieldname):
			continue
		table = f"tab{doctype}"
		stale = f"`{fieldname}` IS NOT NULL AND `{fieldname}` != '' AND `{fieldname}` NOT LIKE '%::%'"
		n = frappe.db.sql(f"SELECT COUNT(*) FROM `{table}` WHERE {stale}")[0][0]
		if not n:
			continue
		frappe.db.sql(f"UPDATE `{table}` SET `{fieldname}` = NULL WHERE {stale}")
		print(f"  blank_free_text_in_link_columns: {doctype}.{fieldname} — {n} row(s) cleared")
	frappe.db.commit()
