# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""What a Smart Setup may carry, recipe by recipe. This registry is closed: a recipe is the only place a
setup's scope is written, and the engine (`bundle`) reads nothing else.

A recipe names its root doctype and two ways a record joins its bundle:
  carries     doctypes the walk follows a Link into, from each root and from every record it reaches;
  companions  doctypes whose every Link lands in the bundle (a grain of the bundle's own axes, a stage of its
              program): they point AT the setup, so no Link from a root reaches them.

A Link to a doctype no recipe carries (a User, a WhatsApp template) is never moved: it must already exist on
the target, and Check names it when it does not.
"""
import frappe
from frappe import _

from tatva_connect.taxonomy import labels

_GRAIN = frozenset({"CRM Vertical", "CRM Group", "CRM Program"})

RECIPES = {
	"API contract": {
		"root": "CRM Lead API Mapping",
		"carries": _GRAIN | {"CRM Lead API Mapping", "CRM Lead API Field", "CRM Lead Section", "CRM Lead Source",
		                     labels.PICKLIST_VALUE},
		"companions": ("CRM Grain", "CRM Lead Stage"),
	},
	"Task type": {
		"root": "CRM Task Type",
		"carries": _GRAIN | {"CRM Task Type", "CRM Task Section", labels.PICKLIST_VALUE},
		"companions": ("CRM Grain",),
	},
	"Web form": {
		"root": "CRM Intake Form",
		"carries": _GRAIN | {"CRM Intake Form", "CRM Lead Source"},
		"companions": ("CRM Grain",),
	},
	"Workflow": {
		"root": "CRM Workflow",
		"carries": _GRAIN | {"CRM Workflow", "CRM Task Type", "CRM Task Section", labels.PICKLIST_VALUE},
		"companions": ("CRM Grain",),
	},
}


def get(key):
	"""One recipe by its name, or a refusal naming it."""
	if key not in RECIPES:
		frappe.throw(_("{0} is not a Smart Setup recipe.").format(key), title=_("Unknown recipe"))
	return RECIPES[key]


def doctypes(recipe):
	"""Every doctype a bundle of this recipe may hold."""
	return recipe["carries"] | set(recipe["companions"])
