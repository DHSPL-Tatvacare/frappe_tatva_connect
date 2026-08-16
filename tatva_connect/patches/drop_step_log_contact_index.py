"""Drop the single-column `contact_index` on CRM Workflow Step Log — `ix_contact_creation (contact, creation)`
strictly dominates it.

WHY IT EXISTED. `contact` carried `search_index: 1` on the doctype, and frappe builds a single-column
`<field>_index` from that flag. The one reader is the contact cap (`automation.contact_cap._contacts_since`),
which counts how often the engine reached one number on a mobile channel inside a ROLLING window.

WHY IT IS DEAD. By leftmost prefix, any query filtering `contact = ?` can use an index that LEADS with
`contact`, so there is no read the narrow index can serve that the composite cannot. For the cap's actual
query the composite is strictly better: it filters `creation > ?` inside the index, where the narrow one
must fetch every candidate row from the table to test the window at all.

WHY IT IS NOT MERELY HARMLESS. Two overlapping indexes cost a write each on every insert, and the step log
takes one row per node per journey — the fastest-growing table in the app. Worse, they give the optimiser a
choice it can get wrong: with both present it was observed picking `contact_index`, i.e. the plan that has
to leave the index to apply the window. Removing the duplicate removes the wrong choice.

The `search_index` flag goes from the doctype JSON in the same change, or sync_fixtures rebuilds this index
on the next migrate and the patch un-does itself for ever.

DDL goes through `_schema.ddl` (patches.txt rule 1), never a raw sql_ddl. Idempotent (has_index guard) and a
free no-op where the index is already gone."""

import frappe

from tatva_connect.patches import _schema

_TABLE = "tabCRM Workflow Step Log"
_INDEX = "contact_index"


def execute():
	if not frappe.db.table_exists("CRM Workflow Step Log"):
		return
	if not frappe.db.has_index(_TABLE, _INDEX):
		return
	# The composite has to be there FIRST: dropping the only index that leads with `contact` would turn
	# every cap check — one per outbound send — into a table scan.
	if not frappe.db.has_index(_TABLE, "ix_contact_creation"):
		return
	try:
		_schema.ddl(f"ALTER TABLE `{_TABLE}` DROP INDEX `{_INDEX}`", _TABLE)
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"workflow_engine: drop index {_INDEX} failed")
