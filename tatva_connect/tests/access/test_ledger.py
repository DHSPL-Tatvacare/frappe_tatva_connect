# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The ledger's own invariants, asserted on the declaration with no site and no data.
The ledger is well-formed, and an undeclared doctype resolves to DENIED."""

from frappe.tests import UnitTestCase

from tatva_connect.access import ledger


def _pad(perms):
	"""A 4-tuple and a 5-tuple ending in 0 declare the same thing; compare them padded."""
	return tuple(perms[:5]) + (0,) * (5 - len(perms[:5]))


class TestLedger(UnitTestCase):
	def test_system_manager_is_never_dropped(self):
		"""A Custom DocPerm overrides the stock matrix wholesale, so omitting System Manager orphans the doctype."""
		for doctype in ledger.OPEN:
			self.assertIn(
				ledger.SYSTEM_MANAGER, ledger.rows_for(doctype), f"{doctype} would lose its admin backstop"
			)

	def test_no_bucket_grants_all_an_unscoped_write(self):
		"""POLICY §5: an `All` write is legitimate only when if_owner scopes it to the caller's own rows."""
		for name, rows in ledger.BUCKETS.items():
			self.assertFalse(ledger.grants_all_write(rows), f"bucket {name} opens every row to everyone")

	def test_the_default_is_denied(self):
		"""The inversion itself — a doctype nobody declared is closed, not left at whatever its app shipped."""
		self.assertEqual(ledger.DEFAULT_BUCKET, "DENIED")
		rows = ledger.rows_for("A Doctype That Does Not Exist")
		self.assertEqual(rows, ledger.BUCKETS["DENIED"])
		self.assertFalse(ledger.is_declared("A Doctype That Does Not Exist"))

	def test_denied_grants_no_write_to_anyone(self):
		"""DENIED must be a resting state, not a soft one: read for an admin, and nothing else at all."""
		for role, perms in ledger.BUCKETS["DENIED"].items():
			self.assertEqual(_pad(perms), (1, 0, 0, 0, 0), f"DENIED grants {role} more than read")

	def test_tier0_is_a_watchlist_not_a_grant(self):
		"""Tier 0 names the escalation set — an entry here must not be opened without a recorded review."""
		for doctype in ledger.TIER0:
			if doctype in ledger.TIER0_REVIEWED:
				continue
			self.assertFalse(
				ledger.is_declared(doctype), f"{doctype} is Tier 0 and must not be opened without review"
			)
