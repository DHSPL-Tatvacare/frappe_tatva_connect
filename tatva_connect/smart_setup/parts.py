# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The parts a grain is set up from, in go-live order: the one declaration Copy, Export, Import and Check read."""
from typing import Callable, NamedTuple

import frappe
from frappe import _

from tatva_connect.authoring import lifecycle
from tatva_connect.taxonomy import grain

DONE, OFF, MISSING, NOT_SET_UP = "Done", "Switched Off", "Missing", "Not Set Up"
READY, DRAFT, DISABLED, SWITCHED_OFF = "Ready", "Draft", "Disabled", "Switched off"


class Part(NamedTuple):
	label: str  # the Desk sidebar name
	doctype: str  # the record a person opens for this part
	step: str  # the New Grain Go-Live page that sets it up
	gather: dict | None  # filters a bundle gathers it by grain with; None when it is reached another way or stays by hand
	carried: bool  # whether a bundle carries it, or it stays by hand
	lands: Callable | None  # (record) -> how a copied record lands, after setting it so
	live: Callable  # (axes) -> (status, what was found)
	optional: bool = False


# -- how a copied record lands -------------------------------------------------

def _ready(record):
	return READY


def _contract_lands(record):
	if record.get("is_internal"):
		return READY
	record.update(partner_user=None, enabled=0)  # one login holds one contract
	return SWITCHED_OFF


def _form_lands(record):
	record.update(lifecycle_state=lifecycle.DRAFT, enabled=0)
	return DRAFT


def _pool_lands(record):
	record.update(disabled=1, credits=None, last_user=None)
	return DISABLED


def _workflow_lands(record):
	return DRAFT  # the canvas saves a new workflow as a Draft


def _intake_lands(record):
	record.update(enabled=0, route=None, web_form_doctype=None)  # rebuilt when the form is published
	return SWITCHED_OFF


# -- whether a part is live on a grain -----------------------------------------

def _counted(on, off, noun):
	"""DONE when something is on, OFF when only switched-off rows exist, else MISSING, with what was found."""
	if on or off:
		return (DONE if on else OFF), _("{0}: {1} on, {2} off").format(noun, on, off)
	return MISSING, _("{0}: none").format(noun)


def _grain_live(axes):
	name = "::".join(value or "" for value in axes)
	return (DONE, name) if frappe.db.exists("CRM Grain", name) else (MISSING, _("No grain row for {0}").format(name))


def _contracts_live(axes):
	rows = grain.rows_covering("CRM Lead API Mapping", axes, fields=("name", "enabled", "is_internal"), every=True, sitewide=False)
	internal = [r for r in rows if r.is_internal]
	return _counted(sum(r.enabled for r in internal), sum(not r.enabled for r in internal), _("Internal contracts"))


def _stages_live(axes):
	program = axes[2]
	if not program:
		return NOT_SET_UP, _("The grain names no program, so its leads have no stages")
	rows = frappe.get_all("CRM Lead Stage", filters={"program": program}, fields=["selectable", "is_conversion_point"])
	if not rows:
		return MISSING, _("No stages for {0}").format(program)
	sale = sum(r.is_conversion_point for r in rows)
	return DONE, _("Stages: {0} pickable, {1} marking the sale").format(sum(r.selectable for r in rows), sale)


def _picklists_live(axes):
	found = len(grain.rows_covering("CRM Picklist Value", axes, every=True))
	return (DONE, _("Options: {0}").format(found)) if found else (NOT_SET_UP, _("Options: none"))


def _forms_live(axes):
	rows = grain.rows_covering("CRM Task Type", axes, fields=("name", "enabled"), every=True, sitewide=False)
	return _counted(sum(r.enabled for r in rows), sum(not r.enabled for r in rows), _("Task forms"))


def _people_live(axes):
	"""People whose own permissions reach this grain; someone who sees every grain is not set up for this one."""
	from tatva_connect.access import entitlement

	users = frappe.get_all("User", filters={"enabled": 1, "user_type": "System User"}, pluck="name")
	found = sum(entitlement.entitled_grains(u) != entitlement.ALL_GRAINS and entitlement.grain_entitled(axes, user=u)
	            for u in users)
	return (DONE, _("People entitled: {0}").format(found)) if found else (MISSING, _("Nobody is entitled to this grain"))


def _pools_live(axes):
	rows = grain.rows_covering("Assignment Rule", axes, {"document_type": "CRM Lead"}, ("name", "disabled"), every=True, sitewide=False)
	return _counted(sum(not r.disabled for r in rows), sum(r.disabled for r in rows), _("Pools"))


def _workflows_live(axes):
	rows = grain.rows_covering("CRM Workflow", axes, fields=("name", "lifecycle_state"), every=True, sitewide=False)
	active = sum(r.lifecycle_state == lifecycle.ACTIVE for r in rows)
	return _counted(active, len(rows) - active, _("Workflows"))


def _routed(resolve, axes):
	"""The account a grain routes to, or the reason it routes to none."""
	try:
		return resolve(frappe._dict(custom_vertical=axes[0], custom_group=axes[1], custom_current_program=axes[2])), None
	except frappe.ValidationError as e:
		frappe.clear_messages()
		return None, str(e)  # a tie between two routing rows is the operator's to fix, so it is reported, not raised


def _whatsapp_live(axes):
	from tatva_connect.whatsapp import routing

	account, why = _routed(routing.resolve_account_for_lead, axes)
	return (DONE, _("Sends from {0}").format(account)) if account else (MISSING, why or _("No routing row, or its account is off"))


def _telephony_live(axes):
	from tatva_connect.telephony import routing

	account, why = _routed(routing.resolve_account_for_lead, axes)
	if not account:
		return MISSING, why or _("No routing row, or its account is off")
	reps = frappe.db.count("CRM Telephony Agent Extension", {"telephony_account": account})
	return (DONE if reps else OFF), _("Calls through {0}. Rep extensions: {1}").format(account, reps)


def _intake_live(axes):
	rows = grain.rows_covering("CRM Intake Form", axes, fields=("name", "enabled"), every=True, sitewide=False)
	status, found = _counted(sum(r.enabled for r in rows), sum(not r.enabled for r in rows), _("Intake forms"))
	return (NOT_SET_UP, found) if status == MISSING else (status, found)


def _views_live(axes):
	found = len(grain.rows_covering("CRM Smart View", axes, {"is_standard": 1}, every=True, sitewide=False))
	return (DONE, _("Standard views: {0}").format(found)) if found else (NOT_SET_UP, _("Standard views: none"))


PARTS = (
	Part(_("Grains"), "CRM Grain", "the-grain", None, True, None, _grain_live),
	Part(_("API Mappings"), "CRM Lead API Mapping", "the-field-contract", {}, True, _contract_lands, _contracts_live),
	Part(_("Lead Stages"), "CRM Lead Stage", "stages-and-picklists", None, True, _ready, _stages_live),
	Part(_("Picklist Values"), "CRM Picklist Value", "stages-and-picklists", None, True, _ready, _picklists_live, True),
	Part(_("Task Types"), "CRM Task Type", "task-types-and-forms", {}, True, _form_lands, _forms_live),
	Part(_("Users and Permissions"), "User Permission", "roles-and-entitlement", None, False, None, _people_live),
	Part(_("Pools"), "Assignment Rule", "assignment-rules", {"document_type": "CRM Lead"}, True, _pool_lands, _pools_live),
	Part(_("Workflows"), "CRM Workflow", "workflows", {}, True, _workflow_lands, _workflows_live),
	Part(_("WhatsApp Routing"), "CRM WhatsApp Routing", "whatsapp", None, False, None, _whatsapp_live),
	Part(_("Telephony Routing"), "CRM Telephony Routing", "telephony", None, False, None, _telephony_live),
	Part(_("Intake Forms"), "CRM Intake Form", "smart-views-and-layout", {}, True, _intake_lands, _intake_live, True),
	Part(_("Smart Views"), "CRM Smart View", "smart-views-and-layout", {"is_standard": 1}, True, _ready, _views_live, True),
)
BY_DOCTYPE = {part.doctype: part for part in PARTS}


def gathered():
	"""{doctype: filters} for every part a bundle gathers by grain."""
	return {part.doctype: part.gather for part in PARTS if part.gather is not None}


def label(doctype):
	"""The part a doctype belongs to, by its Desk name, or the doctype itself for a record no part names."""
	part = BY_DOCTYPE.get(doctype)
	return part.label if part else _(doctype)


def lands(record):
	"""How a copied record lands, after setting it so."""
	part = BY_DOCTYPE.get(record["doctype"])
	return part.lands(record) if part and part.lands else READY


def by_hand():
	"""[{label, step, route}] for every part a bundle never carries."""
	return [_described(part) for part in PARTS if not part.carried]


def check(grain_row):
	"""[{part, status, found, next_step, step, route}] for every part, read live for one grain row."""
	row = frappe.get_cached_doc("CRM Grain", grain_row)
	axes = tuple(row.get(axis) or "" for axis in grain.AXES)
	rows = []
	for part in PARTS:
		status, found = part.live(axes)
		rows.append({**_described(part), "part": part.label, "status": status, "found": found,
		             "next_step": _next_step(part, status)})
	return rows


def _described(part):
	return {"label": part.label, "step": part.step, "route": f"/desk/{frappe.scrub(part.doctype).replace('_', '-')}"}


def _next_step(part, status):
	if status == DONE:
		return ""
	if status == OFF:
		return _("Turn it on in {0} once it is signed off.").format(part.label)
	if status == NOT_SET_UP and part.optional:
		return _("Optional. Set it up in {0} if this grain uses it.").format(part.label)
	return _("Set it up in {0}.").format(part.label)
