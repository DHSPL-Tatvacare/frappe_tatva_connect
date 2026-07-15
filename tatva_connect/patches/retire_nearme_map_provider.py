"""Retire CRM Maps Settings → Near Me Map Provider. Near Me is now one interactive Google map (the OSM/Leaflet branch of the territory map is gone), so a provider choice for that surface controls nothing — a control that lies is worse than no control. CRM Maps Settings is a Single, so the dead value lives as a row in `tabSingles`, which a JSON field removal never clears. Thumbnail / Dialog providers and the OSM tile URL are untouched: the task mini-maps still use them."""
import frappe

_SINGLE = "CRM Maps Settings"
_DEAD_FIELDS = ("nearme_map_provider",)


def execute():
	for fieldname in _DEAD_FIELDS:
		frappe.db.delete("Singles", {"doctype": _SINGLE, "field": fieldname})
	frappe.clear_cache(doctype=_SINGLE)
