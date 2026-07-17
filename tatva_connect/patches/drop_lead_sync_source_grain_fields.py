"""Drop Lead Sync Source's grain fields: the grain now comes from the contract the source links to
(`api_mapping`), and a second copy on the source would be a rival source of truth. Removing them from
the fixture only stops them shipping — an existing row is untouched — so the delete must be declared.
Idempotent; a site that never had them no-ops.
"""
import frappe

_DEAD = (
	"Lead Sync Source-grain_section",
	"Lead Sync Source-grain_source",
	"Lead Sync Source-grain_vertical",
	"Lead Sync Source-grain_col",
	"Lead Sync Source-grain_group",
	"Lead Sync Source-grain_program",
)


def execute():
	for name in _DEAD:
		if frappe.db.exists("Custom Field", name):
			# Custom Field's own on_trash drops the column through Frappe's schema layer — not raw DDL.
			frappe.delete_doc("Custom Field", name, force=True, ignore_permissions=True)  # authz-ok: tier-a — migration, runs as Administrator at migrate
	frappe.clear_cache(doctype="Lead Sync Source")
	frappe.db.commit()
