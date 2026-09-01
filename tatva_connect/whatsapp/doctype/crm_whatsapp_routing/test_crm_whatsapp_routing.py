# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Which account a lead's WhatsApp goes through — the rule, locked forwards and backwards.

THIS FILE WAS AN EMPTY STUB, and the function it should have been guarding is on the SEND path: every
outbound message resolves through `resolve_account_for_lead`, and a wrong answer there is a patient's
message leaving on another programme's number. It is written now because `_specificity` was extracted
out of it — a refactor with no coverage underneath is a claim, not a change — and the lock is on the
RULE rather than on the shape of the code, so the next refactor is judged the same way.

  MOST SPECIFIC WINS, forwards (Program 4 > Group 2 > Product Line 1).
  A SET AXIS MUST EQUAL; a blank one is a wildcard.
  NO GLOBAL DEFAULT — an all-blank rule is refused by the controller.
  AN INACTIVE ACCOUNT IS NOT SELECTABLE, so its rules never win.
  LEAST SPECIFIC WINS, backwards — an account's broadest rule is its catchment.

THE TIE IS UNREACHABLE, and that is asserted rather than assumed. The weights are powers of two, so a
score names exactly WHICH axes a rule sets; two rules matching one lead at one score therefore set the
same axes, and each set axis equals the lead's value — so their triples are identical, which the
controller already refuses. The engine's `frappe.throw` on a tie stays as the backstop it is.

Nothing is sent and no provider is touched: this is resolution only.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect import routing as engine
from tatva_connect.whatsapp import routing

_V, _G, _P = "_TC Route Line", "_TC Route Group", "_TC Route Program"
_BROAD, _NARROW = "_TC Route Broad Account", "_TC Route Narrow Account"


class _RoutingCase(FrappeTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		for dt, name in (("CRM Vertical", _V), ("CRM Group", _G), ("CRM Program", _P)):
			if not frappe.db.exists(dt, name):
				frappe.get_doc({"doctype": dt, "name": name}).insert(
					ignore_permissions=True, ignore_if_duplicate=True
				)
		for account in (_BROAD, _NARROW):
			if not frappe.db.exists("WhatsApp Account", account):
				frappe.get_doc({
					"doctype": "WhatsApp Account", "name": account, "account_name": account,
					"status": "Active", "custom_provider": "WATI",
				}).insert(ignore_permissions=True, ignore_if_duplicate=True)

	def _rule(self, account, vertical=None, group=None, program=None):
		return frappe.get_doc({
			"doctype": "CRM WhatsApp Routing", "whatsapp_account": account,
			"vertical": vertical, "psp_group": group, "program": program,
		}).insert(ignore_permissions=True)

	def _lead(self, vertical=_V, group=_G, program=_P):
		return frappe._dict(
			custom_vertical=vertical, custom_group=group, custom_current_program=program
		)


class TestTheSendPathPicksTheRightAccount(_RoutingCase):
	def test_the_most_specific_rule_wins(self):
		self._rule(_BROAD, vertical=_V)
		self._rule(_NARROW, vertical=_V, group=_G, program=_P)
		self.assertEqual(routing.resolve_account_for_lead(self._lead()), _NARROW)

	def test_a_programme_beats_a_vertical_and_a_group_together(self):
		"""Program is 4 and vertical+group is 3 — the ordering the weights encode."""
		self._rule(_BROAD, vertical=_V, group=_G)
		self._rule(_NARROW, program=_P)
		self.assertEqual(routing.resolve_account_for_lead(self._lead()), _NARROW)

	def test_a_blank_axis_is_a_wildcard(self):
		self._rule(_BROAD, vertical=_V)
		self.assertEqual(routing.resolve_account_for_lead(self._lead(program="_TC Absent")), _BROAD)

	def test_a_set_axis_that_does_not_match_disqualifies_the_rule(self):
		self._rule(_BROAD, vertical=_V, group=_G)
		self.assertIsNone(routing.resolve_account_for_lead(self._lead(group="_TC Other")))

	def test_a_lead_no_rule_covers_resolves_to_nothing(self):
		"""No global default. The caller blocks rather than route through the wrong tenant."""
		self._rule(_BROAD, vertical=_V)
		self.assertIsNone(routing.resolve_account_for_lead(self._lead(vertical="_TC Other Line")))

	def test_an_inactive_accounts_rule_never_wins(self):
		self._rule(_BROAD, vertical=_V)
		self._rule(_NARROW, vertical=_V, group=_G, program=_P)
		frappe.db.set_value("WhatsApp Account", _NARROW, "status", "Inactive")
		self.assertEqual(routing.resolve_account_for_lead(self._lead()), _BROAD)

	def test_an_all_blank_rule_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._rule(_BROAD)

	def test_a_duplicate_triple_is_refused(self):
		"""The guard that makes an ambiguous tie unreachable through the UI."""
		self._rule(_BROAD, vertical=_V, group=_G)
		with self.assertRaises(frappe.ValidationError):
			self._rule(_NARROW, vertical=_V, group=_G)


class TestSpecificityIsOneRule(_RoutingCase):
	"""The extracted weighting, asserted as the property the whole design leans on."""

	def test_a_score_names_exactly_which_axes_a_rule_sets(self):
		"""Powers of two. This is WHY an equal score means an equal triple, and so why a tie cannot occur."""
		seen = {}
		for axes in (
			{}, {"vertical": _V}, {"psp_group": _G}, {"program": _P},
			{"vertical": _V, "psp_group": _G}, {"vertical": _V, "program": _P},
			{"psp_group": _G, "program": _P}, {"vertical": _V, "psp_group": _G, "program": _P},
		):
			rule = frappe._dict(vertical=None, psp_group=None, program=None, **axes)
			score = engine._specificity(rule)
			self.assertNotIn(score, seen, f"{axes} and {seen.get(score)} share a score")
			seen[score] = axes

	def test_the_broadest_rule_is_the_accounts_catchment(self):
		"""Backwards: what a lead must carry to reach this account."""
		self._rule(_BROAD, vertical=_V)
		self._rule(_BROAD, vertical=_V, group=_G, program=_P)
		self.assertEqual(routing.grain_for_account(_BROAD), (_V, "", ""))

	def test_an_account_with_no_rule_declares_no_grain(self):
		self.assertIsNone(routing.grain_for_account(_BROAD))

	def test_two_equally_broad_rules_declare_no_single_grain(self):
		"""Two catchments; picking one would be the guess reading the rules exists to avoid."""
		self._rule(_BROAD, vertical=_V)
		self._rule(_BROAD, psp_group=_G)
		self.assertIsNone(routing.grain_for_account(_BROAD))

	def test_an_inactive_account_declares_no_grain(self):
		"""Its own traffic resolves to nothing, so a lead born at its grain could never be reached."""
		self._rule(_BROAD, vertical=_V)
		frappe.db.set_value("WhatsApp Account", _BROAD, "status", "Inactive")
		self.assertIsNone(routing.grain_for_account(_BROAD))

	def test_the_grain_a_broad_rule_declares_resolves_back_to_that_account(self):
		"""The round trip the enrolment feature depends on, asserted on the resolver itself."""
		self._rule(_BROAD, vertical=_V)
		vertical, group, program = routing.grain_for_account(_BROAD)
		self.assertEqual(routing.resolve_account_for_grain(vertical, group, program), _BROAD)
