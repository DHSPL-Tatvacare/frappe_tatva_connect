# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Grain filter options are drawn from the SCOPED lead table, never the master.

The leak this closes: the lead list is scoped by native User Permission, but the value dropdowns beside
it are fed by frappe's Link search, which is called with the target doctype and no `reference_doctype` —
so our narrow CRM-Lead-scoped permission never fires and the picker offers the whole master. The rep could
not open other business lines' leads, but could read their names.

LAYER 1 of two: self-made fixtures, rolled back, each fails first. It proves the SCOPING RULE — that the
options follow what the user may read — without asserting real tuple counts, which on an empty test DB
would pass by being empty. "The Anaya rep sees exactly their four programmes and not ten" is LAYER 2,
recorded as Gate 3 bench + browser evidence.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.lead.test_grain_filter_options
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.lead.filters import (
	grain_filter_fields,
	grain_filter_options,
	stamp_grain_options,
)

V_MINE, V_THEIRS = "ZZ Filter Vertical Mine", "ZZ Filter Vertical Theirs"
G_MINE, G_THEIRS = "ZZ Filter Group Mine", "ZZ Filter Group Theirs"
P_MINE_1, P_MINE_2, P_THEIRS = "ZZ Filter Prog A", "ZZ Filter Prog B", "ZZ Filter Prog Theirs"
USER = "zz-filter-rep@example.test"
_LEAD_PREFIX = "ZZ-FILTER-"


class TestGrainFilterOptions(FrappeTestCase):
	def setUp(self):
		for doctype, fieldname, values in (
			("CRM Vertical", "vertical_name", (V_MINE, V_THEIRS)),
			("CRM Group", "group_name", (G_MINE, G_THEIRS)),
			("CRM Program", "program_name", (P_MINE_1, P_MINE_2, P_THEIRS)),
		):
			for value in values:
				self._master(doctype, fieldname, value)
		if not frappe.db.exists("User", USER):
			user = frappe.new_doc("User")
			user.email, user.first_name, user.send_welcome_email = USER, "ZZ Filter", 0
			# Both roles, as a real rep carries: stock crm row-restricts a bare Sales User to their own
			# leads, so a bare fixture would see nothing and this test would "pass" against an empty set.
			user.append_roles("Sales User", "Sales Manager")
			user.insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no session user
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.sql("DELETE FROM `tabCRM Lead` WHERE name LIKE %s", (_LEAD_PREFIX + "%",))
		frappe.db.delete("User Permission", {"user": USER})
		frappe.db.delete("User", {"name": USER})
		for doctype, names in (
			("CRM Program", (P_MINE_1, P_MINE_2, P_THEIRS)),
			("CRM Group", (G_MINE, G_THEIRS)),
			("CRM Vertical", (V_MINE, V_THEIRS)),
		):
			for name in names:
				frappe.db.delete(doctype, {"name": name})
		frappe.db.commit()

	# ---- fixtures -------------------------------------------------------------------------

	def _master(self, doctype, fieldname, value):
		if frappe.db.exists(doctype, value):
			return
		doc = frappe.new_doc(doctype)
		doc.set(fieldname, value)
		doc.insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no session user

	def _lead(self, suffix, vertical, group, program):
		"""A raw lead row: the endpoint reads the lead table through get_list, so a SQL fixture is exactly
		what it consumes and avoids dragging the CRM Lead save hooks (grain stamp, dedup) into a test about
		filter options."""
		frappe.db.sql(
			"""INSERT INTO `tabCRM Lead`
			   (name, creation, modified, modified_by, owner, docstatus, idx,
			    custom_vertical, custom_group, custom_current_program)
			   VALUES (%s, NOW(6), NOW(6), 'Administrator', 'Administrator', 0, 0, %s, %s, %s)""",
			(_LEAD_PREFIX + suffix, vertical, group, program),
		)

	def _permit(self, allow, value):
		up = frappe.new_doc("User Permission")
		up.user, up.allow, up.for_value = USER, allow, value
		up.applicable_for = "CRM Lead"
		up.apply_to_all_doctypes = 0
		up.insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no session user

	def _both_worlds(self):
		self._lead("mine-1", V_MINE, G_MINE, P_MINE_1)
		self._lead("mine-2", V_MINE, G_MINE, P_MINE_2)
		self._lead("theirs", V_THEIRS, G_THEIRS, P_THEIRS)
		frappe.db.commit()

	# ---- the rule -------------------------------------------------------------------------

	def test_a_scoped_user_sees_only_their_own_grain_values(self):
		"""THE fix: the options follow the rows the caller may read, so another line's names never appear."""
		self._both_worlds()
		self._permit("CRM Vertical", V_MINE)
		self._permit("CRM Group", G_MINE)
		frappe.db.commit()

		frappe.set_user(USER)
		options = grain_filter_options()
		frappe.set_user("Administrator")

		self.assertEqual(options["custom_vertical"], [V_MINE])
		self.assertEqual(options["custom_group"], [G_MINE])
		self.assertEqual(sorted(options["custom_current_program"]), [P_MINE_1, P_MINE_2])

	def test_the_other_lines_values_are_absent_not_merely_unselectable(self):
		"""Info-disclosure is the point: the name must not reach the browser at all."""
		self._both_worlds()
		self._permit("CRM Vertical", V_MINE)
		self._permit("CRM Group", G_MINE)
		frappe.db.commit()

		frappe.set_user(USER)
		options = grain_filter_options()
		frappe.set_user("Administrator")

		flat = [v for values in options.values() for v in values]
		for leaked in (V_THEIRS, G_THEIRS, P_THEIRS):
			self.assertNotIn(leaked, flat)

	def test_a_wildcard_entitlement_still_gets_every_programme_it_covers(self):
		"""The case a permission-based filter cannot serve: the user holds NO programme permission, so
		asking the permission system would offer nothing. Asking the lead table offers both programmes."""
		self._both_worlds()
		self._permit("CRM Vertical", V_MINE)
		self._permit("CRM Group", G_MINE)
		frappe.db.commit()

		frappe.set_user(USER)
		programmes = grain_filter_options()["custom_current_program"]
		frappe.set_user("Administrator")

		self.assertEqual(sorted(programmes), [P_MINE_1, P_MINE_2])

	def test_an_unscoped_caller_sees_every_value_present_on_a_lead(self):
		"""No special case for a System Manager — the same rule over a wider set of rows."""
		self._both_worlds()
		options = grain_filter_options()  # Administrator
		self.assertIn(V_MINE, options["custom_vertical"])
		self.assertIn(V_THEIRS, options["custom_vertical"])
		self.assertIn(P_THEIRS, options["custom_current_program"])

	def test_a_real_master_with_no_lead_is_not_offered(self):
		"""The FILTER side reads what EXISTS. `P_THEIRS` is a REAL CRM Program row that no lead points at,
		so a master-fed picker would offer it and this filter must not — filtering by it matches nothing.
		(The create picker reads the registry instead, which is why a brand-new programme is still
		creatable — decision 7.) The unused master must be real, or this test proves nothing: an earlier
		draft asserted a name that existed nowhere and stayed green against a master-fed implementation."""
		self._lead("mine-1", V_MINE, G_MINE, P_MINE_1)
		frappe.db.commit()
		self.assertTrue(frappe.db.exists("CRM Program", P_THEIRS), "the unused master must really exist")

		options = grain_filter_options()  # Administrator: unscoped
		self.assertIn(P_MINE_1, options["custom_current_program"])
		self.assertNotIn(P_THEIRS, options["custom_current_program"])

	def test_blank_axes_are_not_offered_as_a_value(self):
		"""A lead before programme enrolment carries a blank programme; blank is not a filter choice."""
		self._lead("blank-prog", V_MINE, G_MINE, None)
		frappe.db.commit()
		options = grain_filter_options()
		self.assertNotIn(None, options["custom_current_program"])
		self.assertNotIn("", options["custom_current_program"])

	def test_a_record_type_with_no_grain_answers_nothing_rather_than_refusing(self):
		"""The contract is TOTAL. A gate here named Lead and Deal, and every OTHER list page and activity
		tab — Tasks, Notes, Calls — mounts the same shared filter, so each one asked and each one got a
		500 on mount. A task carries no grain column, and the truthful answer to "which programmes may I
		offer for a task" is none, not an exception. The derivation is the allowlist."""
		for doctype in ("CRM Task", "FCRM Note", "CRM Call Log", "Contact"):
			self.assertEqual(grain_filter_options(doctype), {}, f"{doctype} must answer, not throw")

	def test_a_catalog_field_carries_its_own_scoped_values(self):
		"""The values ride ON the field, stamped by `fieldname`, so a surface that keys its rows by
		something else is scoped too. A Smart View calls this column `lead:program`; the fieldname match
		this replaced could never reach it, and that surface served the whole programme master."""
		self._both_worlds()
		self._permit("CRM Vertical", V_MINE)
		self._permit("CRM Group", G_MINE)
		frappe.db.commit()

		catalog = [
			{"field_key": "lead:program", "fieldname": "custom_current_program", "fieldtype": "Link"},
			{"field_key": "lead:owner", "fieldname": "lead_owner", "fieldtype": "Link"},
		]
		frappe.set_user(USER)
		stamped = stamp_grain_options(catalog)
		frappe.set_user("Administrator")

		self.assertEqual(sorted(stamped[0]["grain_options"]), [P_MINE_1, P_MINE_2])
		self.assertNotIn(P_THEIRS, stamped[0]["grain_options"])
		# Not an axis: returned exactly as it came, so a control keeps whatever it already rendered.
		self.assertNotIn("grain_options", stamped[1])

	def test_a_stamp_never_mutates_the_catalog_it_was_given(self):
		"""Native caches its catalog answer; stamping in place would write one caller's visible values
		into a cache every caller reads."""
		self._both_worlds()
		field = {"fieldname": "custom_current_program", "fieldtype": "Link"}
		stamp_grain_options([field])
		self.assertNotIn("grain_options", field)

	def test_every_grain_axis_is_answered(self):
		"""Answered for EVERY grain axis the meta declares — asserted against the rule, not a list of
		names. Naming the axes here would re-create the gap that hid the two history Links."""
		self._both_worlds()
		options = grain_filter_options()
		self.assertEqual(sorted(options), sorted(grain_filter_fields()))
		self.assertTrue(options, "the meta must declare at least one grain axis")
