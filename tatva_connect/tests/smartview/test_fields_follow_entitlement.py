# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""GRAIN ENTITLEMENT DECIDES THE FIELDS — a view may narrow it, never widen it.

THE DEFECT THIS LOCKS. Fixing SV-01 (a vertical-wide view was offered and then refused) removed the
authoring clamp from the READ path, and with it the entitlement gate: `_grains_for_view` returned the
VIEW's declared grain, so a view scoped to a whole vertical resolved its columns against that vertical
rather than against the caller. Measured on a real rep: `get_data` shipped 28 columns including
`lead:status` with the value "Nurture" on all 17 rows, while the same rep's `field_catalog` held 55
fields and no `lead:status`. Two consequences, one cause:
  * a rep saw a field their entitlement withholds;
  * filter and sort read the catalog, so that column could not be filtered or sorted — the picker
    honestly could not find "Status", which is how the divergence surfaced.

The rule is the one the app already had: entitlement decides which fields a user may see, and a
surface's own grain may only NARROW that set. One home for it — `entitlement.entitled_grains_within`.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.smartview.test_fields_follow_entitlement
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.access import entitlement
from tatva_connect.smartview import api as smartview

PREFIX = "ZZ FieldGate View"
V1, V2 = "ZZ FG Vertical One", "ZZ FG Vertical Two"
G1, G2 = "ZZ FG Group One", "ZZ FG Group Two"
P1 = "ZZ FG Program One"
REP = "zz-fieldgate-rep@example.com"
# The field the bench actually leaked. Any catalog key would do; this one keeps the test tied to the case.
FIELD = "lead:status"


class _FieldGateCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		for dt, field, names in (("CRM Vertical", "vertical_name", (V1, V2)),
		                         ("CRM Group", "group_name", (G1, G2)),
		                         ("CRM Program", "program_name", (P1,))):
			for name in names:
				if not frappe.db.exists(dt, name):
					frappe.get_doc({"doctype": dt, field: name}).insert(ignore_permissions=True)
		if not frappe.db.exists("User", REP):
			frappe.get_doc({"doctype": "User", "email": REP, "first_name": "FieldGate Rep",
			                "send_welcome_email": 0, "roles": [{"role": "Sales User"}]}
			               ).insert(ignore_permissions=True)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		cls._purge()
		for dt, name in (("User", REP), ("CRM Vertical", V1), ("CRM Vertical", V2),
		                 ("CRM Group", G1), ("CRM Group", G2), ("CRM Program", P1)):
			if frappe.db.exists(dt, name):
				frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	@staticmethod
	def _purge():
		for name in frappe.get_all("CRM Smart View", filters={"label": ["like", f"{PREFIX}%"]}, pluck="name"):
			frappe.delete_doc("CRM Smart View", name, force=True, ignore_permissions=True)

	def setUp(self):
		frappe.set_user("Administrator")
		self._purge()
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		self._purge()
		frappe.db.commit()

	def _view(self, tag, vertical="", group="", program=""):
		return frappe.get_doc({
			"doctype": "CRM Smart View", "label": f"{PREFIX} {tag}", "base_object": "Lead",
			"is_standard": 1, "vertical": vertical or None, "group": group or None,
			"program": program or None, "columns": frappe.as_json([]),
		}).insert(ignore_permissions=True)

	def _entitled(self, grains):
		return patch.object(entitlement, "entitled_grains", return_value=grains)


class TestAViewNarrowsTheCallerNeverWidensThem(_FieldGateCase):
	def test_a_vertical_wide_view_resolves_fields_against_the_caller_not_the_vertical(self):
		"""THE BUG. RED on the old `_grains_for_view`, which returned {(V1, '', '')} — the view's own
		grain — so the field set was the whole vertical's rather than this rep's."""
		v = self._view("vertical wide", vertical=V1)
		mine = {(V1, G1, P1)}
		with self._entitled(mine):
			self.assertEqual(smartview._grains_for_view(v), mine)

	def test_a_view_declaring_no_axis_leaves_the_caller_untouched(self):
		"""Site-wide is a rule about ANY record, not about none — it must narrow nothing (this is what
		keeps a shared cross-line view openable, SV-V5)."""
		v = self._view("site wide")
		mine = {(V1, G1, P1)}
		with self._entitled(mine):
			self.assertEqual(smartview._grains_for_view(v), mine)

	def test_a_view_on_another_line_yields_no_fields_rather_than_that_lines_fields(self):
		"""Fail-closed. The caller may have been admitted by a share, but they hold no grain in that line,
		so the honest answer is nothing — never the other line's columns."""
		v = self._view("other line", vertical=V2, group=G2)
		with self._entitled({(V1, G1, P1)}):
			self.assertEqual(smartview._grains_for_view(v), set())

	def test_an_operator_still_resolves_every_field(self):
		v = self._view("operator", vertical=V1)
		with self._entitled(entitlement.ALL_GRAINS):
			self.assertEqual(smartview._grains_for_view(v), entitlement.ALL_GRAINS)

	def test_another_groups_contract_does_not_leak_through_a_wide_view(self):
		"""THE MECHANISM, driven at the seam that decides it.

		`resolve_fields` admits a field through `overlaps`, where a BLANK axis on the asking side is a
		wildcard. Asking with the author's `(V1, blank, blank)` therefore matched a contract belonging to
		a different group entirely — measured on the bench as `lead:status` reaching a
		`(Goodflip, India, Inside-Sales)` rep via the `(Goodflip, Insurers, Niva-Bupa)` contract. Asking
		with the reader's own grain cannot: `Insurers != India`.

		Ticks are stubbed rather than seeded, so the test states the rule instead of depending on which
		contracts a bench happens to carry — the earlier version of this test compared two empty sets on
		throwaway grains and passed on the broken code."""
		v = self._view("wide view", vertical=V1)
		other_group_contract = {(V1, G2, P1): {FIELD}}
		reader = {(V1, G1, P1)}
		with patch.object(entitlement, "_internal_ticks", return_value=other_group_contract):
			# The author's own grain DOES reach it — which is why the old read path shipped it.
			self.assertTrue(entitlement.entitled_to_field(FIELD, {(V1, "", "")}))
			# The reader's grain does not, and the reader's grain is what a read must ask.
			self.assertFalse(entitlement.entitled_to_field(FIELD, reader))
			with self._entitled(reader):
				self.assertFalse(entitlement.entitled_to_field(FIELD, smartview._grains_for_view(v)),
				                 "a wide view still resolves another group's contract field")
