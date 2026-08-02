"""The ONE read accessor — every gated automation asks `is_enabled("key")`."""
import frappe

from tatva_connect.automation.registry import parent_of


def is_enabled(key: str) -> bool:
	"""On iff the row AND every ancestor it declares is on — the hierarchy is enforced here, nowhere else.

	CACHED, and a flip still takes effect at once: frappe clears the document cache on BOTH write paths,
	`doc.save()` and `db.set_value` (database.py:993), and this codebase uses the latter 81 times. There
	is no stale window to reason about, which is what the previous uncached read was protecting.

	Uncached it cost one SELECT per check, and the wildcard doc_event makes EVERY save ask several times:
	measured 13 per CRM Task insert, 11 per File, 3 on a stock ToDo nobody here wrote. Four switches
	account for all of it; the other 57 are never read on a save. One line, 116 call sites, 66% off a save.

	An unknown key returns None -> False: fail-closed dormant. The walk carries no cycle guard because
	`assert_valid_graph` proved the chain terminates at import.
	"""
	while key:
		if not frappe.get_cached_value("CRM Tatva Automation", key, "enabled"):
			return False
		key = parent_of(key)
	return True
