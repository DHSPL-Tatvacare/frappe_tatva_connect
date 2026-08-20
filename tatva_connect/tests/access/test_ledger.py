# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The ledger's own invariants — asserted on the declaration, with no site and no data.

Two lenses:

  * THE LEDGER IS WELL-FORMED. Buckets resolve, tuples are the right shape, and no bucket hands `All`
    an unscoped write. A malformed row here becomes a wrong Custom DocPerm on every site.
  * THE DEFAULT IS CLOSED. An undeclared doctype resolves to DENIED. This is the inversion, and it is
    the single assertion that would fail if someone re-introduced an open default.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.access.test_ledger
"""

from frappe.tests.utils import FrappeTestCase

from tatva_connect.access import ledger


def _pad(perms):
	"""A 4-tuple and a 5-tuple ending in 0 declare the same thing; compare them padded."""
	return tuple(perms[:5]) + (0,) * (5 - len(perms[:5]))


class TestLedger(FrappeTestCase):
	def test_every_open_entry_resolves(self):
		"""A bucket name must name a real bucket, and explicit rows must be a {role: tuple} map."""
		for doctype, entry in ledger.OPEN.items():
			if isinstance(entry, str):
				self.assertIn(entry, ledger.BUCKETS, f"{doctype} names an unknown bucket {entry!r}")
			else:
				self.assertIsInstance(entry, dict, f"{doctype} must be a bucket name or a role map")
				self.assertTrue(entry, f"{doctype} declares an empty role map")

	def test_every_tuple_is_well_formed(self):
		"""Four to seven flags, each 0 or 1. A malformed tuple becomes a wrong permission row on every site."""
		everything = list(ledger.BUCKETS.items()) + [(d, ledger.rows_for(d)) for d in ledger.OPEN]
		for owner, rows in everything:
			for role, perms in rows.items():
				self.assertGreaterEqual(len(perms), 4, f"{owner}/{role} has fewer than 4 flags")
				self.assertLessEqual(len(perms), 7, f"{owner}/{role} has more than 7 flags")
				for flag in perms:
					self.assertIn(flag, (0, 1), f"{owner}/{role} has a non-boolean flag")

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
