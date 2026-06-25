"""Grain row-scope for CRM Picklist Value — the `permission_query_conditions` backstop.

The picklist options master is reached on the happy path through the scoped query
(`tatva_connect.taxonomy.picklist.picklist_query`), which derives grain server-side. This
hook is the fail-closed backstop for every OTHER list read (`frappe.client.get_list`,
report view, the generic resource API): it clamps a list of CRM Picklist Value to exactly
the caller's entitled grains, so no list path can enumerate options outside the caller's
scope. A blank grain axis on a row is a global/wildcard option, always in scope.

One brain: the entitled grains come from `access.entitlement.entitled_grains` (the same
source the scoped query and Smart Views use) — never re-derived here.
"""
import frappe

from tatva_connect.access import entitlement

# CRM Picklist Value axis columns (blank = global/wildcard).
_AXES = ("vertical", "group", "program")


def _grain_clause(grain):
	"""SQL for one grain tuple: each axis matches the grain value OR is blank (wildcard).
	Axis values are quoted via frappe.db.escape — never string-interpolated raw."""
	parts = []
	for col, val in zip(_AXES, grain):
		col = "`tabCRM Picklist Value`.`{0}`".format(col)
		if val:
			parts.append("({0} = {1} OR {0} = '' OR {0} IS NULL)".format(col, frappe.db.escape(val)))
		else:
			# entitlement to a blank (wildcard) axis covers any value on that axis.
			pass
	return "(" + " AND ".join(parts) + ")" if parts else "1=1"


def get_picklist_value_permission_query_conditions(user=None):
	"""Clamp a CRM Picklist Value list read to the caller's entitled grains (OR of per-grain
	clauses). System Manager (ALL_GRAINS) -> no restriction; no entitled grains -> only the
	fully-global rows (every axis blank), fail-closed."""
	grains = entitlement.entitled_grains(user)
	if grains == entitlement.ALL_GRAINS:
		return ""
	if not grains:
		# Fail-closed floor: only globally-scoped (all-axes-blank) options.
		cols = ["`tabCRM Picklist Value`.`{0}`".format(c) for c in _AXES]
		return " AND ".join("({0} = '' OR {0} IS NULL)".format(c) for c in cols)
	return " OR ".join(_grain_clause(g) for g in grains)
