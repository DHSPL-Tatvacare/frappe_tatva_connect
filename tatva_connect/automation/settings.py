"""The ONE read accessor — every gated automation asks `is_enabled("key")`."""
import frappe

from tatva_connect.automation.registry import parent_of


def is_enabled(key: str) -> bool:
	# On iff the row AND every ancestor it declares is on — the hierarchy is enforced here and nowhere else.
	# Fresh read (no cache) so a flipped switch takes effect at once. An unknown key
	# returns None -> False: fail-closed dormant.
	# The walk carries no cycle guard: `assert_valid_graph` proved the chain terminates at import.
	while key:
		if not frappe.db.get_value("CRM Tatva Automation", key, "enabled"):
			return False
		key = parent_of(key)
	return True
