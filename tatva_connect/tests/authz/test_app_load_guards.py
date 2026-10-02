# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A guard on an overridden native endpoint never asks more of a regular user than native does.
Administrator skips every permission check, so these run as a real Sales User."""
import inspect

import frappe

from tatva_connect import hooks
from tatva_connect.tests.authz import generator, roster
from tatva_connect.tests.authz.base import AuthzTestCase, set_user

# The bare calls the CRM/LMS shell makes on boot; a broken guard here takes the whole app down.
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

			# A **kwargs wrapper forwards native's contract untouched, so it cannot diverge.
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
		"""A bare get_views answers a regular user and never advertises a doctype they cannot read."""
		guard = frappe.get_attr(hooks.override_whitelisted_methods["crm.api.views.get_views"])
		with set_user(self.sales_user):
			views = guard()
			self.assertIsInstance(views, list)
			for view in views:
				self.assertTrue(
					frappe.has_permission(view.get("dt"), "read"),
					f"get_views leaked a view for `{view.get('dt')}`, which this user cannot read",
				)
