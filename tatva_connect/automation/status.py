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


def _stored_ticks():
	return {row.name: bool(row.enabled) for row in frappe.get_all(DOCTYPE, fields=["name", "enabled"])}


def _row(auto, stored):
	"""The ONE shape and the ONE derivation — both reads below return this, so they cannot disagree."""
	enabled = stored.get(auto.key, False)
	effective = is_enabled(auto.key)
	blocked_by = "" if effective else _dormant_ancestor(auto.key, stored)
	return {
		"key": auto.key,
		"area": area_of(auto.key),
		"enabled": enabled,
		"requires": auto.requires,
		"status": ON if effective else (BROKEN if enabled and blocked_by else OFF),
		"blocked_by": blocked_by if enabled and not effective else "",
	}


@frappe.whitelist()
def switch_state():
	"""Every switch in the catalog with its stored tick and its derived status. Read-only; writes nothing."""
	# Reads the catalog rows, so it requires read on the catalog — the same gate the doctype matrix already sets.
	frappe.has_permission(DOCTYPE, "read", throw=True)
	stored = _stored_ticks()
	return [_row(auto, stored) for auto in AUTOMATIONS]


@frappe.whitelist()
def switch_status(key: str):
	"""ONE switch, same shape and same derivation as `switch_state` — for the Desk FORM header.

	The form cannot answer `broken_dependency` from the document alone: the tick is on the row in front of
	the operator, and whether an ancestor is dormant is a walk up `parent_of`. The list's own read is a
	page-lifetime cache filled by list hooks, so a form reached FROM the list would otherwise paint a
	snapshot older than the save the operator just made (frappe/model/indicator.js:87 consults
	`listview_settings.get_indicator` before its own `doc.enabled` fallback at :107).
	"""
	frappe.has_permission(DOCTYPE, "read", throw=True)
	auto = next((a for a in AUTOMATIONS if a.key == key), None)
	if not auto:
		return {}  # a row the catalog no longer declares; the form falls back to the stored tick
	return _row(auto, _stored_ticks())
