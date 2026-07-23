# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Guard: every grain `entitled_grains()` can emit has a matching is_internal=1 contract.

The contract reader (`field_in_grains_via_contract`) resolves a field by an EXACT grain-tuple lookup into
`_internal_ticks()`. So a grain a principal can hold but that has no is_internal=1 mapping would resolve to
NOTHING — an internal user silently loses every non-universal field. This asserts the seeder's grain world
covers both grain sources a principal can hold: partner mapping rows AND live CRM Lead Assignment Rules.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.access.test_every_emittable_grain_has_a_contract
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.access import internal_contract


def _g(vertical, group, program):
	return (vertical or "", group or "", program or "")


class TestEveryEmittableGrainHasAContract(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		internal_contract.ensure_internal_contracts()

	def _internal_contract_grains(self):
		return {_g(m.vertical, m.crm_group, m.program) for m in frappe.get_all(
			"CRM Lead API Mapping", filters={"is_internal": 1},
			fields=["vertical", "crm_group", "program"],
		)}

	def _emittable_grains(self):
		grains = set()
		# Partner mapping grains — EXACTLY what entitlement._partner_grain can hand a caller: it resolves
		# {"partner_user": user, "enabled": 1}, so a mapping with no partner_user (e.g. the Facebook
		# ingestion contract) is handed to nobody and is not emittable. Mirror that filter, or the guard
		# flags a grain no principal can actually hold.
		for m in frappe.get_all(
			"CRM Lead API Mapping", filters={"is_internal": 0, "enabled": 1, "partner_user": ["is", "set"]},
			fields=["vertical", "crm_group", "program"],
		):
			g = _g(m.vertical, m.crm_group, m.program)
			if any(g):
				grains.add(g)
		# Internal grains a rep holds via a live CRM Lead Assignment Rule.
		for a in frappe.get_all(
			"Assignment Rule", filters={"document_type": "CRM Lead", "disabled": 0},
			fields=["grain_vertical", "grain_group", "grain_program"],
		):
			g = _g(a.grain_vertical, a.grain_group, a.grain_program)
			if any(g):
				grains.add(g)
		return grains

	def test_every_emittable_grain_is_covered_by_a_contract(self):
		contract_grains = self._internal_contract_grains()
		missing = sorted(self._emittable_grains() - contract_grains)
		self.assertEqual(
			missing, [],
			f"grains entitled_grains() can emit with NO is_internal contract to resolve: {missing}",
		)
