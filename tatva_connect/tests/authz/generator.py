# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Idempotent test-data generator. Provisions the full dummy roster (+ creds.json for Playwright),
grain-tagged Assignment Rules, a partner mapping, and 100 dummy CRM Leads + CRM Tasks spread across
the 5 canonical grains. DEV/TEST ONLY — never wired into after_migrate/seeds.

Two lifecycles (TESTS.md §8):
  - In-process tests: call seed(commit=False) in setUpClass; IntegrationTestCase rolls it back per
    class. No teardown needed, nothing is committed.
  - Playwright / real HTTP: a standalone runner calls seed(commit=True) (browser runs on a separate
    connection and can't see an uncommitted txn), then teardown() deletes everything by TAG.

Everything created is tagged `authz-test%` so teardown is unambiguous. Masters are NEVER created
(constitution 4/10) — assert_masters_exist() fails loud if a grain's master isn't seeded.
Field names + mandatory fields verified against live meta (recon 2026-06-26).
"""
import os

import frappe

from tatva_connect.tests.authz import grains, roster

TAG = "authz-test"
_MAPPING = "CRM Lead API Mapping"
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def creds_path():
	return os.path.join(frappe.get_site_path("private", "files"), "authz_creds.json")


def _valid_lead_status():
	"""A real CRM Lead Status name (reqd Link on CRM Lead) — resolved live, never hardcoded."""
	status = frappe.get_all("CRM Lead Status", pluck="name", limit=1)
	if not status:
		frappe.throw("No CRM Lead Status records exist — cannot seed leads.")
	return status[0]


# ---- users ---------------------------------------------------------------------------------------

def _ensure_user(p):
	if not frappe.db.exists("User", p["email"]):
		frappe.get_doc({
			"doctype": "User", "email": p["email"], "first_name": p["persona"],
			"user_type": "System User", "send_welcome_email": 0, "new_password": p["password"],
		}).insert(ignore_permissions=True)
	if p["roles"]:
		frappe.get_doc("User", p["email"]).add_roles(*p["roles"])


# ---- grain plumbing ------------------------------------------------------------------------------

def _ensure_assignment_rule(g, user_email):
	name = "{0}::{1}".format(TAG, g["key"])
	if frappe.db.exists("Assignment Rule", name):
		return name
	frappe.get_doc({
		"doctype": "Assignment Rule", "name": name, "document_type": "CRM Lead",
		"description": "authz test rule for {0}".format(g["key"]),
		"assign_condition": "1", "rule": "Round Robin", "priority": 0, "disabled": 0,
		"grain_vertical": g["vertical"], "grain_group": g["group"], "grain_program": g["program"],
		"users": [{"user": user_email}],
		"assignment_days": [{"day": d} for d in _WEEKDAYS],
	}).insert(ignore_permissions=True)
	return name


def _ensure_partner_mapping(g, partner_email):
	if frappe.db.exists(_MAPPING, {"partner_user": partner_email}):
		return
	frappe.get_doc({
		"doctype": _MAPPING, "partner_user": partner_email, "enabled": 1,
		"vertical": g["vertical"], "crm_group": g["group"], "program": g["program"],
	}).insert(ignore_permissions=True)


# ---- leads + tasks -------------------------------------------------------------------------------

def _seed_leads_and_tasks():
	status = _valid_lead_status()
	leads = 0
	for i in range(100):
		g = grains.GRAINS[i % len(grains.GRAINS)]
		lead_name = "{0}-lead-{1:03d}".format(TAG, i)
		task_title = "{0}-task-{1:03d}".format(TAG, i)
		leads += 1
		# Idempotency is per-record, not coupled: a run that died between the two inserts (commit
		# mode) must still complete the task on the next run.
		lead_id = frappe.db.get_value("CRM Lead", {"lead_name": lead_name}, "name")
		if not lead_id:
			lead = frappe.get_doc({
				"doctype": "CRM Lead", "first_name": lead_name, "lead_name": lead_name,
				"status": status, "mobile_no": "9{0:09d}".format(i),
				"custom_vertical": g["vertical"], "custom_group": g["group"],
				"custom_current_program": g["program"],
			})
			lead.insert(ignore_permissions=True)
			lead_id = lead.name
		if not frappe.db.exists("CRM Task", {"title": task_title}):
			frappe.get_doc({
				"doctype": "CRM Task", "title": task_title,
				"reference_doctype": "CRM Lead", "reference_docname": lead_id, "status": "Todo",
			}).insert(ignore_permissions=True)
	return leads


# ---- creds manifest ------------------------------------------------------------------------------

def _write_creds():
	data = [{"persona": p["persona"], "email": p["email"], "password": p["password"],
	         "grain_key": p["grain_key"]} for p in roster.PERSONAS]
	path = creds_path()
	with open(path, "w") as fh:
		fh.write(frappe.as_json(data))
	return path


# ---- public API ----------------------------------------------------------------------------------

def seed(commit=False):
	"""Provision the full roster + grain rules + 100 leads/tasks + creds.json. Idempotent.

	commit=False (default): safe for IntegrationTestCase (rolled back per class, nothing persists).
	commit=True: persist for Playwright/HTTP (a browser can't see an uncommitted txn) — pair with
	teardown().
	"""
	grains.assert_masters_exist()  # fail loud before writing anything
	for p in roster.PERSONAS:
		_ensure_user(p)
	by_key = {g["key"]: g for g in grains.GRAINS}
	for p in roster.PERSONAS:
		if p["grain_key"]:
			_ensure_assignment_rule(by_key[p["grain_key"]], p["email"])
	_ensure_partner_mapping(grains.GRAINS[0], roster.email("partner"))
	leads = _seed_leads_and_tasks()
	creds = _write_creds()
	if commit:
		frappe.db.commit()
	return {"users": len(roster.PERSONAS), "leads": leads,
	        "tasks": frappe.db.count("CRM Task", {"title": ["like", TAG + "%"]}),
	        "creds_path": creds}


def teardown():
	"""Delete everything tagged authz-test (for the committed Playwright/HTTP lifecycle)."""
	for dt, flt in (("CRM Task", {"title": ["like", TAG + "%"]}),
	                ("CRM Lead", {"lead_name": ["like", TAG + "%"]}),
	                ("Assignment Rule", {"name": ["like", TAG + "%"]})):
		for n in frappe.get_all(dt, filters=flt, pluck="name"):
			frappe.delete_doc(dt, n, ignore_permissions=True, force=True)
	for p in roster.PERSONAS:
		for n in frappe.get_all(_MAPPING, {"partner_user": p["email"]}, pluck="name"):
			frappe.delete_doc(_MAPPING, n, ignore_permissions=True, force=True)
		if frappe.db.exists("User", p["email"]):
			frappe.delete_doc("User", p["email"], ignore_permissions=True, force=True)
	if os.path.exists(creds_path()):
		os.remove(creds_path())
	frappe.db.commit()
