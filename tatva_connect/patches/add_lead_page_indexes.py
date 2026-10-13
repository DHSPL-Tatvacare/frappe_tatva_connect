"""CRM Lead pages read off an index: (vertical, group, modified) for a partner line and (lead_owner, modified) for a rep, dropping lead_owner_index and the LSQ unique."""
import frappe

_DOCTYPE = "CRM Lead"
_TABLE = "tabCRM Lead"
_INDEXES = (
	("ix_lead_line_modified", ("custom_vertical", "custom_group", "modified")),
	("ix_lead_owner_modified", ("lead_owner", "modified")),
)
_OWNER_FLAG = "CRM Lead-lead_owner-search_index"
_LSQ_FIELD = "CRM Lead-custom_lsq_prospect_id"


def execute():
	if not frappe.db.table_exists(_DOCTYPE):
		return
	for name, columns in _INDEXES:
		if frappe.db.has_index(_TABLE, name) or any(not frappe.db.has_column(_DOCTYPE, c) for c in columns):
			continue
		try:
			frappe.db.add_index(_DOCTYPE, list(columns), name)
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"indexes: {name} on {_DOCTYPE} failed")
	unflagged = False
	# Drops go through meta so frappe's own sync makes them and never rebuilds them; the owner drop waits for its composite.
	if frappe.db.exists("Property Setter", _OWNER_FLAG) and frappe.db.has_index(_TABLE, "ix_lead_owner_modified"):
		frappe.delete_doc("Property Setter", _OWNER_FLAG, ignore_permissions=True)  # authz-ok: tier-c — migration patch, no session
		unflagged = True
	if frappe.db.get_value("Custom Field", _LSQ_FIELD, "unique"):
		frappe.db.set_value("Custom Field", _LSQ_FIELD, "unique", 0)
		unflagged = True
	if unflagged:
		frappe.clear_cache(doctype=_DOCTYPE)
		try:
			frappe.db.updatedb(_DOCTYPE)
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"indexes: drop on {_DOCTYPE} failed")
