# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""What the activity-type picker OFFERS is exactly what `upsert_view` ACCEPTS.

THE DEFECT THIS LOCKS. The Smart View editor listed `CRM Task Type` through `frappe.client.get_list` —
a raw generic read that asks no grain and no entitlement — so it offered every type on the site, and
`upsert_view` then refused the ones the caller was not entitled to. The refusal surfaced nowhere: the
dialog stayed open, unchanged, with no toast. A rep picked a value the product showed them and the
button appeared dead.

AND THE GATE WAS ASKING THE WRONG QUESTION. A type's grain is a CONTRACT grain, free to leave an axis
blank meaning ANY; `grain_entitled` takes a real record's DATA grain and its own docstring warns that
handing it a wildcard "would compare that wildcard as the empty string and answer confidently wrong".
It did: a type keyed `<vertical>::<group>::` was refused to a user entitled inside that group. The
author-time predicate is `grain_overlaps_entitlement`, which `activity.api.user_query` already asks of
these same axes.

So the rule is one sentence, and it is the only thing worth locking: for any caller, every type the
picker offers passes the save gate, and the gate is asked the author-time question.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.smartview.test_offered_types_are_accepted
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.activity import api as activity_api
from tatva_connect.smartview import api as sv_api

# A NON-OPERATOR is the whole point. An operator resolves to ALL_GRAINS, where both predicates answer
# True for everything, so a test run as Administrator cannot tell the right question from the wrong one
# — it passes against the defect. This user is entitled to ONE program inside a group that also carries
# wildcard-program types, which is exactly the shape that was being refused in production.
REP = "zz-offered-types-rep@example.com"
RULE = "zz-offered-types-rule"
GRAIN = ("Goodflip-Care", "Anaya", "Nivolumab")


class TestOfferedTypesAreAccepted(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		if not frappe.db.exists("User", REP):
			frappe.get_doc({
				"doctype": "User", "email": REP, "first_name": "Offered Types Rep",
				"send_welcome_email": 0, "roles": [{"role": "Sales User"}],
			}).insert(ignore_permissions=True)
		if not frappe.db.exists("Assignment Rule", RULE):
			frappe.get_doc({
				"doctype": "Assignment Rule", "name": RULE, "document_type": "CRM Lead",
				"assign_condition": "1", "rule": "Round Robin", "disabled": 0,
				"grain_vertical": GRAIN[0], "grain_group": GRAIN[1], "grain_program": GRAIN[2],
				# Every day, because the rule only has to EXIST and carry a grain for entitlement to
				# resolve from it — it is never asked to assign anything here.
				"assignment_days": [{"day": d} for d in
				                    ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")],
				"users": [{"user": REP}],
			}).insert(ignore_permissions=True)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		for dt, name in (("Assignment Rule", RULE), ("User", REP)):
			if frappe.db.exists(dt, name):
				frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user(REP)

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_every_offered_type_passes_the_save_gate(self):
		"""THE contract. Nothing the picker shows may be refused by the gate behind it."""
		offered = activity_api.list_types_for_grain(*GRAIN)
		self.assertTrue(offered)
		for t in offered:
			# Raises PermissionError if the gate disagrees with the picker.
			sv_api._assert_type_entitled(t["name"])

	def test_a_wildcard_program_type_is_accepted_inside_its_group(self):
		"""THE regression. A blank axis on a type means ANY, and must not be read as the empty string.

		RED before the fix: `grain_entitled` compared the type's blank program against this rep's
		`Nivolumab` and refused a type their own group owns."""
		name = frappe.db.get_value(
			"CRM Task Type",
			{"enabled": 1, "vertical": GRAIN[0], "group": GRAIN[1], "program": ""},
			"name",
		)
		if not name:
			self.skipTest("no wildcard-program activity type in the fixture grain")
		sv_api._assert_type_entitled(name)

	def test_a_type_outside_the_entitlement_is_STILL_refused(self):
		"""The other half of widening the gate: `grain_overlaps_entitlement` must not become a yes-man.

		This rep is entitled inside Goodflip-Care/Anaya only. A fully-specified type belonging to another
		business line overlaps nothing they hold, so the gate still refuses it — the fix must not have
		traded a wrong refusal for a wrong acceptance."""
		name = frappe.db.get_value(
			"CRM Task Type",
			{"enabled": 1, "vertical": "Tatvapractice", "program": ["!=", ""]},
			"name",
		)
		if not name:
			self.skipTest("no fully-specified out-of-grain activity type on this site")
		self.assertRaises(frappe.PermissionError, sv_api._assert_type_entitled, name)

	def test_the_picker_never_offers_an_out_of_grain_type(self):
		"""And the picker agrees: nothing from another line appears in this grain's list."""
		offered = {t["name"] for t in activity_api.list_types_for_grain(*GRAIN)}
		foreign = set(frappe.get_all(
			"CRM Task Type", filters={"enabled": 1, "vertical": "Tatvapractice"}, pluck="name"
		))
		self.assertEqual(offered & foreign, set())

	def test_the_rep_is_not_an_operator(self):
		"""Guards the guard: as an operator every predicate answers True and this file proves nothing."""
		from tatva_connect.access import entitlement

		self.assertNotEqual(entitlement.entitled_grains(REP), entitlement.ALL_GRAINS)

	def test_the_picker_refuses_a_grain_the_caller_is_not_entitled_to(self):
		"""Scoped, not merely filtered: the endpoint is a door, so it fails closed on an unentitled grain."""
		frappe.set_user("Guest")
		self.assertRaises(
			frappe.PermissionError,
			activity_api.list_types_for_grain,
			"Tatvapractice", "India", "Field-Sales",
		)

	def test_both_pickers_share_one_body(self):
		"""A lead's picker and a grain's picker must not be able to diverge."""
		self.assertTrue(hasattr(activity_api, "_types_for_grain"))
		import inspect

		for fn in (activity_api.list_types_for_lead, activity_api.list_types_for_grain):
			self.assertIn("_types_for_grain", inspect.getsource(fn))
