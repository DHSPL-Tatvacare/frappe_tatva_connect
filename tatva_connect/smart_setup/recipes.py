# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""What a Smart Setup may carry, recipe by recipe. This registry is closed: a recipe is the only place a
setup's scope is written, and the engine (`bundle`) reads nothing else.

A recipe names its root doctype and three ways a record joins its bundle:
  carries     doctypes the walk follows a Link into, from each root and from every record it reaches;
  companions  doctypes whose every Link lands in the bundle (a grain of the bundle's own axes, a stage of its
              program): they point AT the setup, so no Link from a root reaches them;
  members     doctypes gathered by grain, with their filters: every record whose grain is exactly the root's.

A Link to a doctype no recipe carries (a User, a WhatsApp template) is never moved: it must already exist on
the target, and Check names it when it does not.
"""
import frappe
from frappe import _

from tatva_connect.smart_setup import parts
from tatva_connect.taxonomy import labels

_GRAIN = frozenset({"CRM Vertical", "CRM Group", "CRM Program"})
_FIELDS = {"CRM Lead API Field", "CRM Lead Section", "CRM Lead Source", "CRM Task Section", labels.PICKLIST_VALUE}
WHOLE_GRAIN = "Whole Grain"

RECIPES = {
	WHOLE_GRAIN: {
		"root": "CRM Grain",
		"members": parts.gathered(),
		"carries": _GRAIN | {"CRM Grain"} | set(parts.gathered()) | _FIELDS,
		"companions": ("CRM Lead Stage",),
	},
	parts.label("CRM Lead API Mapping"): {
		"root": "CRM Lead API Mapping",
		"carries": _GRAIN | {"CRM Lead API Mapping", "CRM Lead API Field", "CRM Lead Section", "CRM Lead Source",
		                     labels.PICKLIST_VALUE},
		"companions": ("CRM Grain", "CRM Lead Stage"),
	},
	parts.label("CRM Task Type"): {
		"root": "CRM Task Type",
		"carries": _GRAIN | {"CRM Task Type", "CRM Task Section", labels.PICKLIST_VALUE},
		"companions": ("CRM Grain",),
	},
	parts.label("CRM Intake Form"): {
		"root": "CRM Intake Form",
		"carries": _GRAIN | {"CRM Intake Form", "CRM Lead Source"},
		"companions": ("CRM Grain",),
	},
	parts.label("CRM Workflow"): {
		"root": "CRM Workflow",
		"carries": _GRAIN | {"CRM Workflow", "CRM Task Type", "CRM Task Section", labels.PICKLIST_VALUE},
		"companions": ("CRM Grain",),
	},
}
# A bundle built before the recipes took their Desk names still names its recipe the old way.
ALIASES = {"Grain": WHOLE_GRAIN, "API contract": parts.label("CRM Lead API Mapping"), "Task type": parts.label("CRM Task Type"),
           "Web form": parts.label("CRM Intake Form"), "Workflow": parts.label("CRM Workflow")}


def name_of(key):
	"""A recipe's current name, whatever name a setup or bundle stored."""
	return ALIASES.get(key, key)


def get(key):
	"""One recipe by its name or an older name, or a refusal naming it."""
	key = name_of(key)
	if key not in RECIPES:
		frappe.throw(_("{0} is not a Smart Setup recipe.").format(key), title=_("Unknown recipe"))
	return RECIPES[key]


def doctypes(recipe):
	"""Every doctype a bundle of this recipe may hold."""
	return recipe["carries"] | set(recipe["companions"])
