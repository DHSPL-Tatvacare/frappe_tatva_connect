# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The seed asserts what our code depends on and never re-imposes what the operator chose.

Both halves matter and they pull against each other. If the seed re-imposed everything, an operator who
reworded a card's heading would find it reverted on the next deploy and would stop editing anything. If
it asserted nothing, a card re-pointed at the wrong column by an old seed would stay wrong for ever and
nothing would go red. So the line is drawn once, in `_STRUCTURAL`: what a card MEANS is ours, how it
READS is theirs.

Note this seed commits, as every `after_migrate` seed does, so a rolled-back test transaction does not
undo it — each test restores what it changed by hand.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.dashboard.test_seed
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.dashboard import declaration, seed

CHART = declaration.CHART
LAYOUT = declaration.LAYOUT
# The card under test. Any seeded card would do; this one is the simplest to reason about.
SUBJECT = "total_leads"


class SeedCase(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		seed.ensure_rows()
		self.before = frappe.db.get_value(
			CHART, SUBJECT, ("label", "subtitle", *declaration.STRUCTURAL), as_dict=True
		)

	def tearDown(self):
		"""The seed commits, so what a test changed is put back explicitly rather than rolled back."""
		frappe.db.set_value(CHART, SUBJECT, dict(self.before))
		frappe.db.commit()


class TestTheSeedIsIdempotent(SeedCase):
	def test_running_it_twice_adds_nothing(self):
		before = frappe.db.count(CHART)
		seed.ensure_rows()
		self.assertEqual(frappe.db.count(CHART), before)

	def test_every_declared_card_exists(self):
		declared = {row["chart_name"] for row in seed._CHARTS}
		stored = {row.name for row in frappe.get_list(CHART, fields=["name"], limit=0, ignore_permissions=True)}
		self.assertEqual(declared - stored, set())

	def test_the_layouts_that_ship_outrank_a_rep_and_leave_one_role_empty(self):
		"""`Sales User` is left unconfigured on purpose, so the empty state is exercised on a real role.

		TWO layouts, not one: the 56 people holding both `Sales User` and `Sales Manager` landed on the
		rep's, which is the empty one. A manager's layout carries the SAME cards on purpose — what separates
		a manager from an administrator is not the questions asked but the rows the answers are drawn from,
		and that is decided per viewer. So the ORDER is the thing worth asserting, not the count: each sits
		above the next, which is what makes a manager win over the rep and lose to an administrator.
		"""
		roles = [row["role"] for row in seed._LAYOUTS]
		self.assertEqual(roles, ["System Manager", "Sales Manager"])
		priorities = [row["priority"] for row in seed._LAYOUTS]
		self.assertEqual(priorities, sorted(priorities, reverse=True), "a layout must outrank the one below it")
		self.assertNotIn("Sales User", roles, "the empty state has to be exercised on a role somebody holds")


class TestTheOperatorOwnsHowACardReads(SeedCase):
	def test_a_reworded_heading_survives_a_re_run(self):
		frappe.db.set_value(CHART, SUBJECT, "label", "New Patients")
		frappe.db.commit()
		seed.ensure_rows()
		self.assertEqual(frappe.db.get_value(CHART, SUBJECT, "label"), "New Patients")

	def test_a_reworded_subtitle_survives_a_re_run(self):
		frappe.db.set_value(CHART, SUBJECT, "subtitle", "Whatever the operator wants")
		frappe.db.commit()
		seed.ensure_rows()
		self.assertEqual(frappe.db.get_value(CHART, SUBJECT, "subtitle"), "Whatever the operator wants")


class TestOurCodeOwnsWhatACardMeans(SeedCase):
	def test_a_drifted_structural_field_is_put_back(self):
		frappe.db.set_value(CHART, SUBJECT, "date_field", "modified")
		frappe.db.commit()
		seed.ensure_rows()
		self.assertEqual(frappe.db.get_value(CHART, SUBJECT, "date_field"), "creation")

	def test_a_card_re_pointed_at_another_list_is_put_back(self):
		frappe.db.set_value(CHART, SUBJECT, "source_doctype", "CRM Task")
		frappe.db.commit()
		seed.ensure_rows()
		self.assertEqual(frappe.db.get_value(CHART, SUBJECT, "source_doctype"), "CRM Lead")
