# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A filter offers every value the caller's User Permissions let them read, with no lead holding it, and nothing outside them."""
import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.access import entitlement
from tatva_connect.lead.filters import grain_filter_fields, grain_filter_options, stamp_grain_options
from tatva_connect.taxonomy import labels

V_MINE, V_THEIRS = "ZZ Filter Vertical Mine", "ZZ Filter Vertical Theirs"
G_MINE, G_THEIRS = "ZZ Filter Group Mine", "ZZ Filter Group Theirs"
P_MINE_1, P_MINE_2, P_THEIRS = "ZZ Filter Prog A", "ZZ Filter Prog B", "ZZ Filter Prog Theirs"
S_MINE, S_THEIRS = "ZZ Filter Stage Mine", "ZZ Filter Stage Theirs"
USER = "zz-filter-rep@example.test"
FLAG = "Access::Grain::registry"
STAGE = "custom_substage"


class TestGrainFilterOptions(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		for doctype, fieldname, values in (
			("CRM Vertical", "vertical_name", (V_MINE, V_THEIRS)),
			("CRM Group", "group_name", (G_MINE, G_THEIRS)),
			("CRM Program", "program_name", (P_MINE_1, P_MINE_2, P_THEIRS)),
		):
			for value in values:
				if not frappe.db.exists(doctype, value):
					frappe.get_doc({"doctype": doctype, fieldname: value}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture
		if not frappe.db.exists("User", USER):
			user = frappe.new_doc("User")
			user.email, user.first_name, user.send_welcome_email = USER, "ZZ Filter", 0
			user.append_roles("Sales User", "Wiki User")  # wiki adds Wiki User after insert unless present
			user.insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture
		frappe.db.commit()

	def setUp(self):
		frappe.db.savepoint("grain_filter_options")
		self._forget()
		for leaf in ((V_MINE, G_MINE, P_MINE_1), (V_MINE, G_MINE, P_MINE_2), (V_THEIRS, G_THEIRS, P_THEIRS)):
			frappe.get_doc({"doctype": "CRM Grain", "vertical": leaf[0], "group": leaf[1], "program": leaf[2]}).insert(
				ignore_permissions=True)  # authz-ok: tier-c — test fixture, rolled back
		for program, stage in ((P_MINE_1, S_MINE), (P_THEIRS, S_THEIRS)):
			frappe.get_doc({"doctype": "CRM Lead Stage", "program": program, "stage": stage, "selectable": 1}).insert(
				ignore_permissions=True)  # authz-ok: tier-c — test fixture, rolled back

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="grain_filter_options")
		frappe.clear_document_cache("CRM Tatva Automation", FLAG)
		frappe.cache.hdel("user_permissions", USER)  # a rollback fires no hook, so drop the cached copy
		self._forget()

	@staticmethod
	def _forget():
		for bucket in (entitlement._GRAINS_CACHE, entitlement._REGISTRY_FLAG_CACHE, entitlement._REGISTRY_ROWS_CACHE):
			if hasattr(frappe.local, bucket):
				delattr(frappe.local, bucket)

	def _flag(self, enabled):
		row = frappe.get_doc("CRM Tatva Automation", FLAG)
		row.enabled = enabled
		row.save(ignore_permissions=True)  # authz-ok: tier-c — test fixture, rolled back
		self._forget()

	def _permit(self, allow, value):
		frappe.get_doc({"doctype": "User Permission", "user": USER, "allow": allow, "for_value": value,
		                "applicable_for": "CRM Lead", "apply_to_all_doctypes": 0}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture
		self._forget()

	def _as_rep(self, fn, *args):
		frappe.set_user(USER)
		try:
			return fn(*args)
		finally:
			frappe.set_user("Administrator")

	def _my_group(self):
		"""A rep entitled to a whole group, so every programme under it."""
		self._permit("CRM Vertical", V_MINE)
		self._permit("CRM Group", G_MINE)

	# ---- the rule -------------------------------------------------------------------------

	def test_a_rep_is_offered_their_whole_grain_with_no_lead_at_all(self):
		"""No lead exists, and every value of the rep's grain is still offered."""
		self._my_group()
		options = self._as_rep(grain_filter_options)
		self.assertEqual(options["custom_vertical"], [V_MINE])
		self.assertEqual(options["custom_group"], [G_MINE])
		self.assertEqual(options["custom_current_program"], [P_MINE_1, P_MINE_2])
		self.assertIn(S_MINE, options[STAGE], "a stage no lead holds yet must still be offered")

	def test_another_grains_values_never_reach_the_rep(self):
		"""Not one value of the other line, on any axis."""
		self._my_group()
		flat = [v for values in self._as_rep(grain_filter_options).values() for v in values]
		for other in (V_THEIRS, G_THEIRS, P_THEIRS, S_THEIRS):
			self.assertNotIn(other, flat)

	def test_the_stage_picker_and_the_stage_filter_give_one_answer(self):
		"""The type-ahead (`label_query`) reads the same scoped labels as the stamped filter."""
		self._my_group()
		searched = [label for label, _shown in self._as_rep(labels.label_query, labels.LEAD_STAGE, "ZZ Filter Stage", "name", 0, 50, None)]
		self.assertEqual(searched, [S_MINE])

	def test_a_rep_no_grain_permission_narrows_is_offered_every_value(self):
		"""Frappe scopes no lead of theirs by grain, so the filter offers what their list can show."""
		options = self._as_rep(grain_filter_options)
		self.assertIn(V_MINE, options["custom_vertical"])
		self.assertIn(V_THEIRS, options["custom_vertical"])
		self.assertIn(S_THEIRS, options[STAGE])

	def test_the_registry_flag_never_changes_what_a_rep_is_offered(self):
		"""The options follow the User Permissions that scope the list, whichever source field entitlement reads."""
		self._my_group()
		self._flag(0)
		off = self._as_rep(grain_filter_options)
		self._flag(1)
		self.assertEqual(self._as_rep(grain_filter_options), off)
		self.assertEqual(off["custom_current_program"], [P_MINE_1, P_MINE_2])

	def test_an_unscoped_caller_is_offered_every_master_value(self):
		"""System Manager is offered every value the masters hold."""
		options = grain_filter_options()  # Administrator
		self.assertIn(V_THEIRS, options["custom_vertical"])
		self.assertIn(P_THEIRS, options["custom_current_program"])
		self.assertIn(S_THEIRS, options[STAGE])

	def test_a_record_type_with_no_grain_answers_nothing_rather_than_refusing(self):
		"""Every list page mounts the shared filter, so a doctype with no grain Link answers `{}`."""
		for doctype in ("CRM Task", "FCRM Note", "CRM Call Log", "Contact"):
			self.assertEqual(grain_filter_options(doctype), {}, f"{doctype} must answer, not throw")

	def test_a_catalog_field_carries_its_own_scoped_values(self):
		"""Stamped by `fieldname`, so a Smart View's `lead:program` is scoped too."""
		self._my_group()
		catalog = [
			{"field_key": "lead:program", "fieldname": "custom_current_program", "fieldtype": "Link"},
			{"field_key": "lead:owner", "fieldname": "lead_owner", "fieldtype": "Link"},
		]
		stamped = self._as_rep(stamp_grain_options, catalog)
		self.assertEqual(stamped[0]["grain_options"], [P_MINE_1, P_MINE_2])
		self.assertNotIn("grain_options", stamped[1], "not an axis: returned exactly as it came")

	def test_a_stamp_never_mutates_the_catalog_it_was_given(self):
		"""Native caches its catalog answer, so a stamp must never write into it."""
		field = {"fieldname": "custom_current_program", "fieldtype": "Link"}
		stamp_grain_options([field])
		self.assertNotIn("grain_options", field)

	def test_every_grain_axis_is_answered(self):
		"""Every grain axis the meta declares gets a key."""
		options = grain_filter_options()
		self.assertEqual(sorted(options), sorted(grain_filter_fields()))
		self.assertTrue(options, "the meta must declare at least one grain axis")
