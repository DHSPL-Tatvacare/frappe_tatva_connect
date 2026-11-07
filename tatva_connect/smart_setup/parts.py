# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The parts a grain is set up from, in go-live order: the one declaration Copy, Export, Import and Check read."""
from collections.abc import Callable
from functools import partial
from typing import NamedTuple

import frappe
from frappe import _

from tatva_connect.authoring import lifecycle
from tatva_connect.taxonomy import grain

DONE, OFF, MISSING, NOT_SET_UP = "Done", "Switched Off", "Missing", "Not Set Up"
READY, DRAFT, TURNED_ON = "Ready", "Draft", "Turned On"
SWITCHED_OFF = OFF  # one word for the off state, on the checklist and under Lands As


class Landing(NamedTuple):
	state: str  # how the record reads under Lands As
	off: dict  # the fields that hold it off, set when Copy or Import creates it
	on: dict  # the values that mean it is on, read live
	turn_on: Callable  # (doctype, name, on) -> switches it on through its owner's own path


class Left(NamedTuple):
	filters: dict  # what a Copy still gathers: the rest stays on the source grain
	what: str  # what stays, as the Not Carried list names it


class Part(NamedTuple):
	label: str  # the Desk sidebar name
	doctype: str  # the record a person opens for this part
	step: str  # the New Grain Go-Live page that sets it up
	gather: dict | None  # filters a bundle gathers it by grain with; None when it is reached another way or stays by hand
	carried: bool  # whether a bundle carries it, or it stays by hand
	lands: Landing | None  # how a record Copy or Import creates arrives; None when it arrives ready
	live: Callable  # (axes) -> (status, what was found)
	optional: bool = False
	left: Left | None = None  # what a Copy leaves on the source grain: it belongs to that grain only


# -- how a record arrives, and how it is turned on ----------------------------

def _save(doctype, name, on, **flags):
	"""The normal save, so every rule the record's own form runs still runs; `flags` ride on the doc."""
	doc = frappe.get_doc(doctype, name)
	doc.update(on)
	doc.flags.update(flags)
	doc.save()


def _activate(doctype, name, on):
	"""Publish a Draft, then activate, through the lifecycle the builders use; a definition that cannot publish says why."""
	if frappe.db.get_value(doctype, name, "lifecycle_state") == lifecycle.DRAFT:
		answer = lifecycle.publish(doctype, name)
		if not answer["ok"]:
			frappe.throw(answer["summary"], title=_("Cannot publish"))
	lifecycle.transition(doctype, name, lifecycle.ACTIVE)


def _pool_on(doctype, name, on):
	"""A pool turns on only with people in its member grid: a copy never carries them, so a copied pool starts empty."""
	from tatva_connect.lead import assignment_rule

	doc = frappe.get_doc(doctype, name)
	if doc.rule != assignment_rule.BASED_ON_FIELD and not doc.get(assignment_rule.member_grid(doc.rule)):
		frappe.throw(_("Add the people this pool gives records to, then turn it on."), title=_("Pool has nobody in it"))
	_save(doctype, name, on)


_ACTIVE = {"lifecycle_state": lifecycle.ACTIVE}
_CONTRACT = Landing(SWITCHED_OFF, {"enabled": 0, "partner_user": None}, {"enabled": 1}, _save)  # the partner login is added later, on the contract
_FORM = Landing(DRAFT, {"lifecycle_state": lifecycle.DRAFT, "enabled": 0}, _ACTIVE, _activate)
_POOL = Landing(SWITCHED_OFF, {"disabled": 1, "credits": None, "last_user": None}, {"disabled": 0}, _pool_on)
_WORKFLOW = Landing(DRAFT, {}, _ACTIVE, _activate)  # the canvas creates every workflow as a Draft
_INTAKE = Landing(SWITCHED_OFF, {"enabled": 0, "route": None}, {"enabled": 1},  # publishing its Web Form stays on the form
                  partial(_save, sync_after_commit=True))


# -- whether a part is live on a grain -----------------------------------------

def _counted(on, off, noun):
	"""DONE when something is on, OFF when only switched-off rows exist, else MISSING, with what was found."""
	if on or off:
		return (DONE if on else OFF), _("{0}: {1} on, {2} off").format(noun, on, off)
	return MISSING, _("{0}: none").format(noun)


def _split(doctype, rows, axes, is_on):
	"""(on, off): a covering row counts when on, as it runs here; only the grain's own row counts off, as only it is this grain's to turn on."""
	cols = [(col, value or "") for col, value in zip(grain.columns(doctype, every=True), axes, strict=True) if col]
	own = [all(grain.same(row.get(col) or "", value) for col, value in cols) for row in rows]
	return sum(is_on(r) for r in rows), sum(not is_on(r) and mine for r, mine in zip(rows, own, strict=True))


def _grain_live(axes):
	name = "::".join(value or "" for value in axes)
	return (DONE, name) if frappe.db.exists("CRM Grain", name) else (MISSING, _("No grain row for {0}").format(name))


def _contracts_live(axes):
	rows = grain.rows_covering("CRM Lead API Mapping", axes, fields=("name", "enabled", "is_internal"), every=True, sitewide=False)
	internal = [r for r in rows if r.is_internal]
	return _counted(*_split("CRM Lead API Mapping", internal, axes, lambda r: r.enabled), _("Internal contracts"))


def _stages_live(axes):
	program = axes[2]
	if not program:
		return NOT_SET_UP, _("The grain names no program, so its leads have no stages")
	rows = frappe.get_all("CRM Lead Stage", filters={"program": program}, fields=["selectable", "is_conversion_point"])
	if not rows:
		return MISSING, _("No stages for {0}").format(program)
	sale = sum(r.is_conversion_point for r in rows)
	return DONE, _("Stages of {0}, shared by every grain on it: {1} pickable, {2} marking the sale").format(program, sum(r.selectable for r in rows), sale)


def _picklists_live(axes):
	found = len(grain.rows_covering("CRM Picklist Value", axes, every=True))
	return (DONE, _("Options: {0}").format(found)) if found else (NOT_SET_UP, _("Options: none"))


def _forms_live(axes):
	rows = grain.rows_covering("CRM Task Type", axes, fields=("name", "enabled"), every=True, sitewide=False)
	return _counted(*_split("CRM Task Type", rows, axes, lambda r: r.enabled), _("Task forms"))


def _people_live(axes):
	"""People whose own permissions reach this grain; someone who sees every grain is not set up for this one."""
	from tatva_connect.access import entitlement

	users = frappe.get_all("User", filters={"enabled": 1, "user_type": "System User"}, pluck="name")
	found = sum(entitlement.entitled_grains(u) != entitlement.ALL_GRAINS and entitlement.grain_entitled(axes, user=u)
	            for u in users)
	return (DONE, _("People entitled: {0}").format(found)) if found else (MISSING, _("Nobody is entitled to this grain"))


def _pools_live(axes):
	rows = grain.rows_covering("Assignment Rule", axes, {"document_type": "CRM Lead"}, ("name", "disabled"), every=True, sitewide=False)
	return _counted(*_split("Assignment Rule", rows, axes, lambda r: not r.disabled), _("Pools"))


def _workflows_live(axes):
	rows = grain.rows_covering("CRM Workflow", axes, fields=("name", "lifecycle_state"), every=True, sitewide=False)
	return _counted(*_split("CRM Workflow", rows, axes, lambda r: r.lifecycle_state == lifecycle.ACTIVE), _("Workflows"))


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
	status, found = _counted(*_split("CRM Intake Form", rows, axes, lambda r: r.enabled), _("Intake forms"))
	return (NOT_SET_UP, found) if status == MISSING else (status, found)


def _views_live(axes):
	found = len(grain.rows_covering("CRM Smart View", axes, {"is_standard": 1}, every=True, sitewide=False))
	return (DONE, _("Standard views: {0}").format(found)) if found else (NOT_SET_UP, _("Standard views: none"))


PARTS = (
	Part(_("Grains"), "CRM Grain", "the-grain", None, True, None, _grain_live),
	Part(_("API Mappings"), "CRM Lead API Mapping", "the-field-contract", {}, True, _CONTRACT, _contracts_live,
	     left=Left({"is_internal": 1}, _("partner contracts"))),  # a partner's contract belongs to that partner
	Part(_("Lead Stages"), "CRM Lead Stage", "stages-and-picklists", None, True, None, _stages_live),
	Part(_("Picklist Values"), "CRM Picklist Value", "stages-and-picklists", None, True, None, _picklists_live, True),
	Part(_("Task Types"), "CRM Task Type", "task-types-and-forms", {}, True, _FORM, _forms_live),
	Part(_("Users and Permissions"), "User Permission", "roles-and-entitlement", None, False, None, _people_live),
	Part(_("Pools"), "Assignment Rule", "assignment-rules", {"document_type": "CRM Lead"}, True, _POOL, _pools_live),
	Part(_("Workflows"), "CRM Workflow", "workflows", {}, True, _WORKFLOW, _workflows_live),
	Part(_("WhatsApp Routing"), "CRM WhatsApp Routing", "whatsapp", None, False, None, _whatsapp_live),
	Part(_("Telephony Routing"), "CRM Telephony Routing", "telephony", None, False, None, _telephony_live),
	Part(_("Intake Forms"), "CRM Intake Form", "smart-views-and-layout", {}, True, _INTAKE, _intake_live, True),
	Part(_("Smart Views"), "CRM Smart View", "smart-views-and-layout", {"is_standard": 1}, True, None, _views_live, True),
)
BY_DOCTYPE = {part.doctype: part for part in PARTS}


def gathered():
	"""{doctype: filters} for every part a bundle gathers by grain."""
	return {part.doctype: part.gather for part in PARTS if part.gather is not None}


def copied(members):
	"""`members` as a Copy gathers them: each part's `left` filters added, so what belongs to one grain only stays behind."""
	return {doctype: {**filters, **(_left(doctype).filters if _left(doctype) else {})} for doctype, filters in members.items()}


def _left(doctype):
	part = BY_DOCTYPE.get(doctype)
	return part and part.left


def label(doctype):
	"""The part a doctype belongs to, by its Desk name, or the doctype itself for a record no part names."""
	part = BY_DOCTYPE.get(doctype)
	return part.label if part else _(doctype)


def lands(record, current=None):
	"""How a record Copy or Import writes arrives, after setting it so: a new one switched off, an existing one as this site has it."""
	landing = _landing(record["doctype"])
	if not landing:
		return READY
	if current is None:
		record.update(landing.off)
		return landing.state
	record.update({field: current.get(field) for field in landing.off if field in current})
	return None


def turn_on(doctype, name):
	"""Switch on one record a setup brought in, through its owner's own path."""
	landing = _landing(doctype)
	landing.turn_on(doctype, name, landing.on)


def is_on(doctype, name):
	"""Whether a record that lands switched off is on now, read live; None when it is gone or arrives ready."""
	landing = _landing(doctype)
	values = landing and frappe.db.get_value(doctype, name, list(landing.on), as_dict=True)
	return all(values[field] == value for field, value in landing.on.items()) if values else None


def _landing(doctype):
	part = BY_DOCTYPE.get(doctype)
	return part and part.lands


def by_hand():
	"""[{label, step, route}] for every part a copy never carries, and what a carried part leaves behind, in go-live order."""
	return [{**_described(part), "label": f"{part.label}: {part.left.what}" if part.carried else part.label}
	        for part in PARTS if not part.carried or part.left]


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
