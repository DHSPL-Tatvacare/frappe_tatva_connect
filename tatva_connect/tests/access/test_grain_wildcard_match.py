# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""THE grain-match rule, locked: a BLANK axis on a contract is a WILDCARD, never the empty string.

A contract is declared at the level visibility is granted — a rep sees all of Goodflip-Care/Anaya
whatever program the patient enrolled into — so its program axis is deliberately blank. The DATA always
carries all three axes. An earlier build compared the two with an exact tuple lookup, so a lead whose
program was set matched no contract and its whole Data tab collapsed to the universal keys. Every other
matcher in the app (taxonomy.grain._score, automation.rules.grain_matches, workflow_engine triggers,
activity.api) already implements blank-as-wildcard; this locks the contract matcher to the same rule.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.access.test_grain_wildcard_match
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.access import entitlement, internal_contract
from tatva_connect.taxonomy.grain import resolve_scoped


class TestContractCoversRule(FrappeTestCase):
	"""The predicate itself — pure, no DB."""

	def test_blank_axis_is_a_wildcard(self):
		# contract (V, G, blank) covers a lead on ANY program of that vertical+group
		self.assertTrue(entitlement._contract_covers(("V", "G", ""), ("V", "G", "P1")))
		self.assertTrue(entitlement._contract_covers(("V", "G", ""), ("V", "G", "P2")))
		self.assertTrue(entitlement._contract_covers(("V", "", ""), ("V", "G", "P1")))
		self.assertTrue(entitlement._contract_covers(("", "", ""), ("V", "G", "P1")))

	def test_set_axis_must_equal(self):
		self.assertTrue(entitlement._contract_covers(("V", "G", "P1"), ("V", "G", "P1")))
		self.assertFalse(entitlement._contract_covers(("V", "G", "P1"), ("V", "G", "P2")))
		self.assertFalse(entitlement._contract_covers(("V2", "G", ""), ("V", "G", "P1")))

	def test_agrees_with_the_canonical_brain(self):
		"""taxonomy.grain is the reference implementation — the contract matcher must not diverge."""
		axes = ("V", "G", "P1")
		for cg in [("V", "G", "P1"), ("V", "G", ""), ("V", "", ""), ("", "", ""),
		           ("V", "G", "P2"), ("V2", "G", "P1")]:
			canonical = resolve_scoped(
				[{"vertical": cg[0], "group": cg[1], "program": cg[2]}], *axes
			) is not None
			self.assertEqual(
				entitlement._contract_covers(cg, axes), canonical,
				f"contract {cg} vs {axes}: diverged from taxonomy.grain",
			)


class TestVisibilityThroughContract(FrappeTestCase):
	"""End to end against the live contracts: a program-set grain resolves its vertical+group contract."""

	def test_program_set_grain_sees_its_blank_program_contract(self):
		ticks = entitlement._internal_ticks()
		blank_program = [g for g in ticks if g[0] and g[1] and not g[2] and ticks[g]]
		if not blank_program:
			self.skipTest("no vertical+group contract with a blank program on this site")
		cg = blank_program[0]
		key = sorted(ticks[cg])[0]
		# a lead of that vertical+group enrolled into ANY program must still see the field
		self.assertTrue(
			entitlement.field_in_grains_via_contract(key, [(cg[0], cg[1], "Some Enrolled Program")]),
			"a blank program axis must cover every program — this is the regression that emptied the Data tab",
		)

	def test_other_vertical_is_still_denied(self):
		"""The fix must not open anything: a foreign grain still sees nothing."""
		ticks = entitlement._internal_ticks()
		any_keys = [ticks[g] for g in ticks if ticks[g]]
		if not any_keys:
			self.skipTest("no internal contracts seeded on this site")
		key = sorted(any_keys[0])[0]
		self.assertFalse(
			entitlement.field_in_grains_via_contract(key, [("ZZ Not A Vertical", "ZZ Not A Group", "ZZ")])
		)


class TestRowKeyTravelsWithItsSection(FrappeTestCase):
	"""The seeder RULE: granting a multi-row section's values also grants the row key that dates them.
	Driven with SYNTHETIC catalog sets against whatever section declares a row key — the rule is
	asserted, never the seed's field list (that is operator data; a reseed must not turn a test red)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.section, cls.row_key = None, None
		for s in frappe.get_all("CRM Lead Section", fields=["name", "row_key_field"]):
			if s.row_key_field:
				cls.section, cls.row_key = s.name, s.row_key_field
				break
		cls.keyless = next(
			(s.name for s in frappe.get_all("CRM Lead Section", fields=["name", "row_key_field"])
			 if not s.row_key_field), None)

	def _key(self, fieldname):
		return frappe.db.get_value(
			"CRM Lead API Field", {"section": self.section, "fieldname": fieldname}, "field_key")

	def test_granting_a_section_also_grants_its_row_key(self):
		if not self.section:
			self.skipTest("no section declares a row key on this site")
		rk = self._key(self.row_key)
		self.assertTrue(rk, "the row key must itself be catalogued")
		self.assertEqual(internal_contract._row_key_keys({self.section}, {rk}), {rk})

	def test_a_row_key_absent_from_the_catalog_is_not_invented(self):
		if not self.section:
			self.skipTest("no section declares a row key on this site")
		self.assertEqual(internal_contract._row_key_keys({self.section}, set()), set())

	def test_a_section_with_no_row_key_adds_nothing(self):
		if not self.keyless:
			self.skipTest("every section declares a row key on this site")
		self.assertEqual(internal_contract._row_key_keys({self.keyless}, {"anything:at:all"}), set())
