# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The overridden-native guards, driven as a REGULAR user — the actor a bench never runs as.

Every entry in `override_whitelisted_methods` replaces a native endpoint the CRM/LMS frontend calls.
A wrapper that is STRICTER than the function it replaces cannot fail on a developer's bench: Frappe
short-circuits every permission check for Administrator, so the wrapper's gate is never reached. It
fails for the first real user who opens the app, and it fails everywhere at once.

That is not hypothetical. `native_guards.get_views` gated on `has_permission(doctype, throw=True)`
while native treats `doctype` as OPTIONAL and the frontend calls it bare on app load. The blank
doctype reached `get_meta("")` -> DoesNotExistError -> HTTP 404 on every page for every
non-Administrator, and UAT was unusable while every local test stayed green. This module is the wall.

PART A — `TestGuardSignatureParity` (the source lock, every override, including ones not yet written):
  A parameter that is OPTIONAL in native must be OPTIONAL in ours, and ours must accept everything
  native accepts. A guard may narrow what a caller SEES; it may never narrow what a caller may ASK.

PART B — `TestAppLoadAsRegularUser` (the live lock, the bare calls the frontend makes on load):
  Driven as a real Sales User. A PermissionError is a PASS — that is the gate doing its job. A
  DoesNotExistError or TypeError is the failure: the guard broke on a call it was never meant to
  refuse.
"""
import inspect

import frappe

from tatva_connect import hooks
from tatva_connect.tests.authz import generator, roster
from tatva_connect.tests.authz.base import AuthzTestCase, set_user

# The calls the CRM/LMS shell makes with no arguments as it boots. These are the ones that take the
# whole app down when a guard is wrong, because nothing renders until they return.
BARE_APP_LOAD = [
	("crm.api.views.get_views", ()),
	("crm.api.views.get_views", ("",)),
	("crm.api.assignment_rule.get_assignment_rules_list", ()),
	("lms.lms.utils.get_courses", ()),
	("lms.lms.utils.get_batches", ()),
]


class TestGuardSignatureParity(AuthzTestCase):
	def test_no_guard_is_stricter_than_the_native_it_replaces(self):
		offenders = []
		for native_path, ours_path in hooks.override_whitelisted_methods.items():
			if "." not in native_path:
				continue  # a bare handler alias (`upload_file`); its dotted twin is in this same map and is checked
			native_params = inspect.signature(frappe.get_attr(native_path)).parameters
			our_params = inspect.signature(frappe.get_attr(ours_path)).parameters

			# A **kwargs wrapper forwards native's own contract untouched — nothing to diverge.
			if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in our_params.values()):
				continue

			for name, native_param in native_params.items():
				if native_param.kind in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL):
					continue
				ours = our_params.get(name)
				if ours is None:
					offenders.append(f"{ours_path}: drops `{name}`, which {native_path} accepts")
				elif native_param.default is not inspect.Parameter.empty and ours.default is inspect.Parameter.empty:
					offenders.append(f"{ours_path}: `{name}` is REQUIRED here but OPTIONAL in {native_path}")

		self.assertEqual(
			offenders, [],
			"a guard asks more of its caller than the native endpoint it replaces:\n  " + "\n  ".join(offenders),
		)


class TestAppLoadAsRegularUser(AuthzTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		generator.seed(commit=False)
		cls.sales_user = roster.email("grain_1")  # a plain Sales User — never Administrator

	def test_bare_app_load_calls_do_not_break_for_a_regular_user(self):
		broken = []
		for native_path, args in BARE_APP_LOAD:
			guard = frappe.get_attr(hooks.override_whitelisted_methods[native_path])
			with set_user(self.sales_user):
				try:
					guard(*args)
				except frappe.PermissionError:
					pass  # a refusal is the gate working; only a crash is a defect
				except Exception as exc:
					broken.append(f"{native_path}{args} -> {type(exc).__name__}: {exc}")

		self.assertEqual(
			broken, [],
			"an app-load endpoint crashed for a regular user (it would 404 the whole CRM):\n  " + "\n  ".join(broken),
		)

	def test_get_views_bare_returns_only_readable_doctypes(self):
		"""The exact UAT 404: a bare get_views must answer, and must not advertise an unreadable doctype."""
		guard = frappe.get_attr(hooks.override_whitelisted_methods["crm.api.views.get_views"])
		with set_user(self.sales_user):
			views = guard()
			self.assertIsInstance(views, list)
			for view in views:
				self.assertTrue(
					frappe.has_permission(view.get("dt"), "read"),
					f"get_views leaked a view for `{view.get('dt')}`, which this user cannot read",
				)
