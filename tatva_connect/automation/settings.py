"""The ONE read accessor — every gated automation asks `is_enabled("key")`."""
import frappe


def is_enabled(key: str) -> bool:
	# Fresh read (no cache) so a flipped switch takes effect at once. An unknown key
	# returns None -> False: fail-closed dormant.
	return bool(frappe.db.get_value("CRM Tatva Automation", key, "enabled"))
