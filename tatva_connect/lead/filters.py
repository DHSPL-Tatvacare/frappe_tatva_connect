"""Grain filter options: every value the caller's User Permissions let them read, from the masters, never from their leads."""
import frappe
from frappe.utils.caching import redis_cache

from tatva_connect.access import entitlement
from tatva_connect.taxonomy import grain, labels

# The masters that make a Link a grain axis; which fields point at them is read off the meta.
GRAIN_MASTERS = tuple(grain.master(axis) for axis in grain.AXES)

# Keyed by a grain, so stamped as the caller's labels; CRM Picklist Value stays a type-ahead scoped by its own query.
GRAIN_KEYED = ("CRM Lead Stage",)

_DOCTYPE = "CRM Lead"

# Per user, an hour: the set moves only when a master, the registry or the user's permissions do, never with lead activity.
_OPTIONS_TTL = 60 * 60


def is_axis(target: str) -> bool:
	"""Whether a Link at `target` is scoped here."""
	return target in GRAIN_MASTERS or target in GRAIN_KEYED


def grain_filter_fields(doctype: str = _DOCTYPE):
	"""Every field on `doctype` whose Link target is a grain axis, read off the live meta."""
	return tuple(
		field.fieldname
		for field in frappe.get_meta(doctype).fields
		if field.fieldtype == "Link" and is_axis(field.options)
	)


@frappe.whitelist()
@redis_cache(ttl=_OPTIONS_TTL, user=True)
def grain_filter_options(doctype: str = _DOCTYPE):
	"""`{fieldname: [values]}` for every grain axis on `doctype`; `{}` for a doctype with none."""
	meta = frappe.get_meta(doctype)
	return {fieldname: offered(meta.get_field(fieldname).options) for fieldname in grain_filter_fields(doctype)}


def offered(target):
	"""What a filter on a Link at `target` may offer: the axis's readable values, or a grain-keyed master's labels within them."""
	if labels.is_composite(target):
		return labels.labels_of(target)
	return entitlement.readable_values(grain.axis_of(target))


def stamp_grain_options(fields, doctype: str = _DOCTYPE):
	"""Each grain axis in a field catalog, as a new dict carrying `grain_options`; every other field untouched."""
	values = grain_filter_options(doctype) or {}  # the cache can answer None where the function answers {}
	return [
		{**f, "grain_options": values[f.get("fieldname")]} if f.get("fieldname") in values else f
		for f in fields
	]
