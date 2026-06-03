"""Public geo lookups for the enrolment form (guest-safe, read-only)."""
import frappe
from frappe.utils import cint


@frappe.whitelist(allow_guest=True)
def search_cities(state=None, query="", limit=20):
	"""Live city search for the State→City cascade. Returns up to `limit` city names
	in `state` matching `query`. Guarded: needs a state + 2+ chars; capped; names only."""
	state = (state or "").strip()
	query = (query or "").strip()
	if not state or len(query) < 2:
		return []
	return frappe.get_all(
		"CRM City",
		filters={"state": state, "city_name": ["like", f"%{query}%"]},
		pluck="city_name",
		order_by="city_name asc",
		limit=min(cint(limit) or 20, 50),
	)
