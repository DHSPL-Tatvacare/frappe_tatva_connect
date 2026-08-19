"""`plan_includes_cgm` became a Select; the Check's old default `0` is not an answer, so it is cleared.

The column shipped as a Check with default "0", and NOTHING ever wrote it — `mx_Plan_includes_CGM` was
absent from every mapping, so no lead has ever carried a real value. It is a Select `Yes`/`No` now,
because that is what the LSQ form a rep submits actually draws, and `0` is not one of its options: every
plan-profile row is refused on save with 'cannot be "0"', which blocks EDITING THE LEAD AT ALL.

Cleared rather than read as `No`: 4,798 rows on this bench hold the default, and calling that "No" would
invent an answer for every one of them. A field nobody has answered reads blank; the real values arrive
with the Goodflip re-migration, which now maps mx_Plan_includes_CGM.

Touches ONLY the two dead Check literals, so a row already holding Yes/No is untouched and a second run
is a no-op. No DDL — the fieldtype change is the doctype's own.
"""
import frappe


def execute():
	if not frappe.db.has_column("CRM Plan Profile", "plan_includes_cgm"):
		return
	frappe.db.sql("""
		UPDATE `tabCRM Plan Profile` SET plan_includes_cgm = ''
		 WHERE plan_includes_cgm IN ('0', '1')
	""")
	frappe.db.commit()
