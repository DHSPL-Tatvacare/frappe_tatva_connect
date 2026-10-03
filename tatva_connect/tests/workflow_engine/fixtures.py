# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Builders for the Distribute, shift and check-in suites. Leads, users and pools come from `workflow_engine/tests/fixtures.py`."""

import datetime

import frappe
from frappe.cache_manager import clear_doctype_map
from frappe.tests import IntegrationTestCase

from tatva_connect.automation import actions, rules
from tatva_connect.automation import context as ctx_build
from tatva_connect.tests.authz.grains import GRAINS
from tatva_connect.workflow_engine import ENGINE_SWITCH, refs
from tatva_connect.workflow_engine.tests.fixtures import GRAIN, WEEK, make_lead, make_pool, make_user

REGISTRY_FLAG = "Access::Grain::registry"
OTHER_GRAIN = next(g for g in GRAINS if g["vertical"] != GRAIN["vertical"])
MONDAY = datetime.date(2030, 1, 7)
# Per-request memos on `frappe.local` the draw reads; a test process is one long request, so each test starts a fresh one.
_REQUEST_CACHES = (
	"tatva_connect:pool_holidays", "tatva_connect:shift_hours", "tatva_connect:entitled_grains",
	"tatva_connect:grain_registry_flag", "tatva_connect:grain_registry_rows",
)


class PoolTestCase(IntegrationTestCase):
	"""Only this suite's pools can take its leads: inside the rolled-back class, every other enabled CRM Lead rule is disabled and the engine is off.

	`registry` pins where entitlement comes from: 0 is pool membership, 1 is native User Permission."""

	registry = 0

	@classmethod
	def setUpClass(cls):
		# Registered before super(): class cleanups run last in, first out, so these drop redis copies after its rollback.
		cls.addClassCleanup(clear_doctype_map, "Assignment Rule", "CRM Lead")
		cls.addClassCleanup(frappe.clear_document_cache, "Assignment Rule")
		cls.addClassCleanup(frappe.clear_document_cache, "CRM Tatva Automation")
		super().setUpClass()
		# Off, so a lead insert starts no journey, whose segment commit would defeat the rollback.
		set_switch(ENGINE_SWITCH, 0)
		set_switch(REGISTRY_FLAG, cls.registry)
		for name in frappe.get_all("Assignment Rule", {"document_type": "CRM Lead", "disabled": 0}, pluck="name"):
			rule = frappe.get_doc("Assignment Rule", name)
			rule.disabled = 1
			rule.save(ignore_permissions=True)
		clear_doctype_map("Assignment Rule", "CRM Lead")
		fresh_request()

	def setUp(self):
		fresh_request()


def set_switch(key, enabled):
	row = frappe.get_doc("CRM Tatva Automation", key)
	row.enabled = enabled
	row.save(ignore_permissions=True)


def fresh_request():
	for bucket in _REQUEST_CACHES:
		if hasattr(frappe.local, bucket):
			delattr(frappe.local, bucket)


def at(day, hour, minute=0):
	return datetime.datetime.combine(day, datetime.time(hour, minute))


def rep(label, *roles):
	"""A new user each run, so redis keys keyed on their session never carry over from a previous run."""
	user = make_user(f"{label}-{frappe.generate_hash(length=6)}@example.invalid")
	frappe.get_doc("User", user).add_roles(*(roles or ("Sales User",)))
	return user


def shift(start, end, grain=GRAIN):
	"""A work shift at `grain` running `start`–`end` every day; an end before the start crosses midnight."""
	return frappe.get_doc({
		"doctype": "Tatva Work Shift", "shift_name": f"probe {frappe.generate_hash(length=6)}",
		"grain_vertical": grain.get("vertical"), "grain_group": grain.get("group"), "grain_program": grain.get("program"),
		"working_hours": [{"workday": day, "start_time": start, "end_time": end} for day in WEEK],
	}).insert(ignore_permissions=True).name  # authz-ok: tier-c — test fixture, no user input


def pool(members, **overrides):
	"""A Credit Weighted pool drawn from only by Distribute; `members` is `[(user, weight, shift)]` or `[(user, weight, shift, row_extras)]`."""
	rows = [{"user": m[0], "weight": m[1], "work_shift": m[2], **(m[3] if len(m) > 3 else {})} for m in members]
	return make_pool(rows, **{"assigned_by_workflow": 1, **overrides})


def leave(user, day):
	return frappe.get_doc({"doctype": "Tatva User Leave", "user": user, "from_date": day, "to_date": day}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture


def holiday_list(vertical, day):
	return frappe.get_doc({
		"doctype": "CRM Holiday List", "holiday_list_name": f"probe {frappe.generate_hash(length=6)}",
		"from_date": day, "to_date": day + datetime.timedelta(days=30), "grain_vertical": vertical,
		"holidays": [{"date": day, "description": "probe"}],
	}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture


def leads(count):
	return [make_lead() for _ in range(count)]


def distribute(rule, lead, only_when=None):
	"""Run a Distribute node on `lead` as the engine does, through the verb's declared handler; `(output, assigned_to, opens_at)`.

	The engine reaches Distribute only through `interpreter.advance`, which commits; this is the narrowest public door that does not."""
	state = ctx_build.context_for(lead, {})
	params = frappe._dict(assignment_rule=rule.name, only_when=only_when, action_type="Distribute")
	actions.handler_of("Distribute")(params, lead.name, state.writing_as("d"), rules.lead_axes(lead.name), lead)
	return state.get(refs.OUTPUT), state.get("d.assigned_to"), state.get("d.opens_at")


def credits(rule):
	return frappe.parse_json(frappe.get_all("Assignment Rule", {"name": rule.name}, pluck="credits")[0] or "{}")


def latest_checkin(user):
	"""The user's newest check-in row as Desk lists it, or None."""
	rows = frappe.get_all("Tatva User Checkin", {"user": user}, ["status", "source", "owner"], order_by="creation desc", limit=1)
	return rows[0] if rows else None
