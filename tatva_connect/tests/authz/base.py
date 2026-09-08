# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""AuthzTestCase — the single base for every in-process authz test.

Two non-negotiables, wired once here so no test can forget them:
  1. COMMS-OFF interlock — assert_comms_off() at class setup AND per test. If WhatsApp/Acefone/
     follow-up is live, the test ERRORS rather than risk a real send (comms.py).
  2. Rollback — IntegrationTestCase rolls back per CLASS to the single setUpClass commit floor
     (verified: integration_test_case.py commits at setUpClass, then addClassCleanup(_rollback_db)).
     CRITICAL (audit 2026-06-26): a per-TEST `frappe.db.rollback()` unwinds to that SAME floor —
     there is no nested transaction — so it would DESTROY class-seeded fixtures after the first
     test. We therefore do NOT add a per-test rollback. Seed once in setUpClass (commit=False); the
     class rollback is the cleanup. A test that MUTATES shared rows must isolate itself with a named
     savepoint: `frappe.db.savepoint(name)` in setUp / `frappe.db.rollback(save_point=name)` in
     cleanup — never a bare rollback. In-process tests never commit; committed data for Playwright
     is the generator's separate (teardown-by-tag) concern.

`set_user` (Frappe's context manager, restores the prior user) is re-exported for convenience —
we reuse it rather than hand-rolling impersonation, matching the existing suite's pattern.

`dispatch` and `mk_user` live here for the same reason: every suite that drives a guarded native
endpoint needs both, and they were copied into four modules before this. One home, one behaviour.
"""
import frappe
from frappe.tests import IntegrationTestCase, set_user

from tatva_connect.tests.authz.comms import assert_comms_off

__all__ = ["AuthzTestCase", "dispatch", "mk_user", "set_user"]


def dispatch(cmd, **kwargs):
	"""Mirror frappe.handler.execute_cmd's override resolution — the REAL HTTP dispatch path, so
	override_whitelisted_methods (the native_guards wrappers) is honoured. A direct import would bypass
	the wrapper and give a false green."""
	for hook in (frappe.get_hooks("override_whitelisted_methods") or {}).get(cmd, []):
		cmd = hook
		break
	return frappe.call(frappe.get_attr(cmd), **kwargs)


def mk_user(email, roles):
	"""Seed a persona idempotently and give it exactly `roles`. Returns the email, so a caller can
	assign and seed in one line."""
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{"doctype": "User", "email": email, "first_name": email.split("@")[0], "send_welcome_email": 0}
		).insert(ignore_permissions=True)  # authz-ok: tier-a — test persona seeding
	user = frappe.get_doc("User", email)
	user.add_roles(*roles)
	return email


class AuthzTestCase(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()  # REQUIRED — establishes the commit floor + class rollback
		assert_comms_off()    # refuse the whole class if a comms channel is live

	def setUp(self):
		assert_comms_off()  # belt-and-suspenders, per test (no per-test rollback — see module docstring)
