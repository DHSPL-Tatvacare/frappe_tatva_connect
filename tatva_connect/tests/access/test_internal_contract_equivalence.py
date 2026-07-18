# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""THE Phase 5A proof: the per-grain internal contracts reproduce the grain_* visibility EXACTLY.

Before any reader is switched from grain_* to the contract, this proves the switch is a no-op: for
EVERY catalog field and EVERY internal-contract grain, `field_in_grains_via_contract(field_key, {G})`
(the new contract brain) returns the SAME answer as `field_in_grains(field_row, {G})` (the live grain_*
brain). Iterating every field against every grain also exercises the "grain foreign to this field" case
(a field owned by grain A tested against grain B) — the contract must deny it exactly as grain_* does,
while the universal floor stays visible under every grain.

grain_* is NOT removed by Phase 5A; this test reads BOTH brains and asserts they agree.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.access.test_internal_contract_equivalence
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.access import entitlement, internal_contract


class TestInternalContractEquivalence(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# Idempotent re-seed so the proof stands regardless of whether this run followed a migrate.
		internal_contract.ensure_internal_contracts()
		cls.fields = frappe.get_all(
			"CRM Lead API Field",
			fields=["field_key", "grain_vertical", "grain_group", "grain_program"],
			order_by="field_key asc",
		)
		cls.grains = sorted(internal_contract._contract_grains())

	def test_there_are_fields_and_contract_grains(self):
		# Guard against a vacuous pass: no rows / no grains would make every loop below trivially green.
		self.assertTrue(self.fields, "catalog must have fields, else the proof is vacuous")
		self.assertTrue(self.grains, "there must be internal-contract grains, else the proof is vacuous")

	def test_contract_matches_grain_star_for_every_field_and_grain(self):
		mismatches = []
		for row in self.fields:
			for grain in self.grains:
				old = entitlement.field_in_grains(row, {grain})
				new = entitlement.field_in_grains_via_contract(row.field_key, {grain})
				if old != new:
					mismatches.append((row.field_key, grain, old, new))
		self.assertEqual(
			mismatches, [],
			f"contract diverges from grain_* on {len(mismatches)} (field, grain) pairs: {mismatches[:20]}",
		)

	def test_system_manager_all_grains_true_in_both(self):
		# ALL_GRAINS is the System-Manager sentinel; both brains must let it see every field.
		for row in self.fields:
			self.assertTrue(entitlement.field_in_grains(row, entitlement.ALL_GRAINS))
			self.assertTrue(
				entitlement.field_in_grains_via_contract(row.field_key, entitlement.ALL_GRAINS)
			)

	def test_universal_fields_ticked_in_every_contract(self):
		# The universal floor (blank grain on every axis) must be visible under EVERY grain in both
		# brains — this is what keeps a rep's lead-identifying fields when the reader is switched.
		universals = [r.field_key for r in self.fields
			if not (r.grain_vertical or r.grain_group or r.grain_program)]
		self.assertTrue(universals, "expected some universal (blank-grain) fields")
		for key in universals:
			for grain in self.grains:
				self.assertTrue(
					entitlement.field_in_grains_via_contract(key, {grain}),
					f"universal field {key} must be visible under grain {grain}",
				)
