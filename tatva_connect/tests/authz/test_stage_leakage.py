# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Stage / sub-stage grain-isolation tests.

CRM Lead Stage is scoped by PROGRAM (PK `{program}::{stage}`; no vertical/group axis). The resolver
`lead_stages(lead)` returns only the lead's program's stages (+ blank-program wildcards); the
`validate_stage` backstop (gated by the `Lead::CRM Lead::stage` switch) blocks a save whose picked
stage belongs to a different program.

These tests prove a stage for one grain does not leak as a stage for another — where "grain" for
stages means PROGRAM. Note the design fact: grains #4 and #5 both map to program "InsideSales", so
they legitimately SHARE stages — that is expected, NOT leakage, and is asserted explicitly so a
future change can't quietly turn shared-by-design into a real cross-grain leak.

All values are read from the live DB at runtime (no hardcoded stage names). Mutating cases use
named savepoints so the class fixtures survive (base.py rollback discipline).
"""
import frappe

from tatva_connect.lead.leads import lead_stages
from tatva_connect.tests.authz import generator
from tatva_connect.tests.authz.base import AuthzTestCase

STAGE_SWITCH = "Lead::CRM Lead::stage"
# The program each canonical grain maps to (grains.py order). #4 and #5 share "InsideSales".
GRAIN_PROGRAMS = ("Nivolumab", "Tukavo", "FieldSales", "InsideSales")


class TestStageLeakage(AuthzTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		generator.seed()  # provisions 100 leads spread across the 5 grains' programs

	# -- helpers ----------------------------------------------------------------------------------

	def _lead_for_program(self, program):
		return frappe.db.get_value(
			"CRM Lead",
			{"lead_name": ["like", generator.TAG + "%"], "custom_current_program": program},
			"name",
		)

	def _selectable_stage(self, program):
		rows = frappe.get_all(
			"CRM Lead Stage", filters={"program": program, "selectable": 1}, pluck="name", limit=1
		)
		return rows[0] if rows else None

	# -- resolver scoping -------------------------------------------------------------------------

	def test_resolver_returns_only_the_leads_program_stages(self):
		"""lead_stages(lead) must return stages of the lead's program (or blank-program) ONLY."""
		for program in GRAIN_PROGRAMS:
			lead = self._lead_for_program(program)
			with self.subTest(program=program):
				if not lead:
					self.skipTest("no seeded lead for program {0}".format(program))
				returned = lead_stages(lead)
				if not returned:
					self.skipTest("program {0} has no selectable stages on this DB".format(program))
				for s in returned:
					prog = frappe.db.get_value("CRM Lead Stage", s["name"], "program")
					self.assertIn(
						prog, (program, "", None),
						"stage {0} (program {1}) leaked into a {2} lead's picker".format(
							s["name"], prog, program),
					)

	def test_known_cross_program_stage_does_not_leak(self):
		"""A real Tukavo stage must never appear in a Nivolumab lead's resolver (and vice versa)."""
		pairs = (("Nivolumab", "Tukavo"), ("FieldSales", "InsideSales"))
		for own, foreign in pairs:
			with self.subTest(own=own, foreign=foreign):
				lead = self._lead_for_program(own)
				foreign_stage = self._selectable_stage(foreign)
				if not lead or not foreign_stage:
					self.skipTest("need a {0} lead and a {1} selectable stage".format(own, foreign))
				names = {s["name"] for s in lead_stages(lead)}
				self.assertNotIn(
					foreign_stage, names,
					"{0} stage {1} leaked into a {2} lead's picker".format(foreign, foreign_stage, own),
				)

	def test_same_program_grains_share_stages_by_design(self):
		"""grains #4 and #5 both map to program InsideSales -> identical stage sets. EXPECTED (stages
		are program-scoped, not full-grain). Asserted so a change can't silently make it a leak."""
		leads = frappe.get_all(
			"CRM Lead",
			filters={"lead_name": ["like", generator.TAG + "%"], "custom_current_program": "InsideSales"},
			pluck="name", limit=2,
		)
		if len(leads) < 2:
			self.skipTest("need two InsideSales leads (grains #4 and #5)")
		a = {s["name"] for s in lead_stages(leads[0])}
		b = {s["name"] for s in lead_stages(leads[1])}
		self.assertEqual(a, b, "two InsideSales leads see different stage sets — program scoping broke")

	# -- enforcement backstop (validate_stage) ----------------------------------------------------

	def test_cross_program_save_rejected_when_enforced(self):
		"""With Lead::CRM Lead::stage ON, saving a Nivolumab lead with a Tukavo stage must be blocked
		server-side (fail-closed backstop — not a UI-only filter)."""
		lead = self._lead_for_program("Nivolumab")
		foreign = self._selectable_stage("Tukavo")
		if not lead or not foreign:
			self.skipTest("need a Nivolumab lead and a Tukavo selectable stage")
		frappe.db.savepoint("stage_enforced")
		try:
			frappe.db.set_value("CRM Tatva Automation", STAGE_SWITCH, "enabled", 1)
			doc = frappe.get_doc("CRM Lead", lead)
			doc.custom_substage = foreign
			with self.assertRaises(frappe.ValidationError):
				doc.save(ignore_permissions=True)  # ignore_permissions skips perms, NOT validate hooks
		finally:
			frappe.db.rollback(save_point="stage_enforced")

	def test_cross_program_save_allowed_when_switch_off_is_the_documented_exposure(self):
		"""With the switch OFF (the current dev state), there is NO stage enforcement — a cross-program
		stage saves freely. This documents the exposure: the operator MUST keep the switch ON for the
		grain backstop to hold. (Mirrors the visibility switch-OFF delta.)"""
		lead = self._lead_for_program("Nivolumab")
		foreign = self._selectable_stage("Tukavo")
		if not lead or not foreign:
			self.skipTest("need a Nivolumab lead and a Tukavo selectable stage")
		frappe.db.savepoint("stage_off")
		try:
			frappe.db.set_value("CRM Tatva Automation", STAGE_SWITCH, "enabled", 0)
			doc = frappe.get_doc("CRM Lead", lead)
			doc.custom_substage = foreign
			doc.save(ignore_permissions=True)  # NOT blocked — the documented switch-off exposure
			self.assertEqual(
				frappe.db.get_value("CRM Lead", lead, "custom_substage"), foreign,
				"switch OFF should allow the cross-program stage (exposure to document)",
			)
		finally:
			frappe.db.rollback(save_point="stage_off")

	# -- wildcard guard ---------------------------------------------------------------------------

	def test_no_blank_program_wildcard_stages_exist(self):
		"""The resolver treats a blank-program stage as a wildcard visible in EVERY grain's picker.
		`program` is reqd today so none should exist; this guards against a future blank-program row
		silently leaking into every grain."""
		blanks = frappe.get_all("CRM Lead Stage", filters={"program": ["in", ["", None]]}, pluck="name")
		self.assertEqual(
			blanks, [],
			"blank-program (wildcard) stages exist and leak into every grain's picker: {0}".format(blanks),
		)
