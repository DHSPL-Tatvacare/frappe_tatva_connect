"""The READ over the automation catalog: what is on, what is off, and what is armed above a dormant parent.

No second catalog and no state of its own. The rows are `registry.AUTOMATIONS`, the effective answer is
`settings.is_enabled` verbatim, and the stored ticks are one query over the catalog table. Nothing here
re-decides whether a switch runs.

`broken_dependency` is the one silent state this read exists to name: the row's own tick is on, so the
desk shows it armed, while `is_enabled` walks the chain and answers off — the automation does not run and
nothing is logged, because a dormant automation logs nothing by design. `blocked_by` names the nearest
ancestor whose tick is off, so the operator has something to act on rather than a chain to walk by hand.

Reachable whenever a tick is written past the controller — a `db.set_value`, a data import, a fixture, or
a deploy that declares a NEW `requires` over a switch an operator had already armed. `seed._disarm_orphans`
heals that last case at migrate; between deploys, this is what sees it.
"""
import frappe

from tatva_connect.automation.registry import AUTOMATIONS, area_of, parent_of
from tatva_connect.automation.settings import is_enabled

DOCTYPE = "CRM Tatva Automation"

ON = "on"
OFF = "off"
BROKEN = "broken_dependency"


def _dormant_ancestor(key, stored):
	"""Names the culprit only — whether the switch RUNS is `is_enabled`'s answer and is never recomputed here."""
	parent = parent_of(key)
	while parent:
		if not stored.get(parent):
			return parent
		parent = parent_of(parent)
	return ""


@frappe.whitelist()
def switch_state():
	"""Every switch in the catalog with its stored tick and its derived status. Read-only; writes nothing."""
	# Reads the catalog rows, so it requires read on the catalog — the same gate the doctype matrix already sets.
	frappe.has_permission(DOCTYPE, "read", throw=True)
	stored = {row.name: bool(row.enabled) for row in frappe.get_all(DOCTYPE, fields=["name", "enabled"])}
	rows = []
	for auto in AUTOMATIONS:
		enabled = stored.get(auto.key, False)
		effective = is_enabled(auto.key)
		blocked_by = "" if effective else _dormant_ancestor(auto.key, stored)
		rows.append(
			{
				"key": auto.key,
				"area": area_of(auto.key),
				"enabled": enabled,
				"requires": auto.requires,
				"status": ON if effective else (BROKEN if enabled and blocked_by else OFF),
				"blocked_by": blocked_by if enabled and not effective else "",
			}
		)
	return rows
