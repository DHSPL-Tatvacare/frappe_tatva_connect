# TEMPORARY — tests for the migration reconciliation tool. Removed with it; see REMOVE-ME.md.
"""The reconciliation must compare MEANINGS and count through ONE brain, or it lies politely.

Run:
    bench --site wipetest.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.migration_check.test_reconciliation
"""
import inspect

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.migration_check import batch, compare, jobs, totals
from tatva_connect.migration_check import constants as C


class TestComparableValues(FrappeTestCase):
	def test_a_composite_key_compares_equal_to_its_bare_name(self):
		"""A Frappe Link to a grain master stores the composite PK; LeadSquared holds the bare name.
		Every such field read as `differ` — measured on real leads, the whole mapped section lit red."""
		self.assertEqual(
			compare._status("Sigrima", "Goodflip-Care::Anaya::Sigrima"), compare.MATCH,
			"the composite grain key must compare equal to the bare name LeadSquared holds",
		)
		self.assertEqual(
			compare._status("Enrolled", "Goodflip-Care::Anaya::::Enrolled"), compare.MATCH,
			"a FOUR-part key (blank program axis) must also answer with its last segment",
		)

	def test_different_values_still_differ(self):
		"""The normalisation must not manufacture matches."""
		self.assertEqual(compare._status("Sigrima", "Goodflip-Care::Anaya::Nivolumab"), compare.DIFFER)
		self.assertEqual(compare._status("Sigrima", ""), compare.MISSING_IN_CRM)


class TestOneCountingBrain(FrappeTestCase):
	"""The certificate (totals) and the bulk check (batch) must count activities through the SAME
	query, or a lead that tallies on one tab contradicts the other and the operator trusts neither."""

	def test_batch_counts_through_the_totals_brain(self):
		src = inspect.getsource(batch)
		self.assertIn("totals.crm_tasks_by_bare_type", src,
					  "batch must count through the shared brain, not a private query")
		self.assertNotIn("custom_lsq_activity_id", src,
						 "the provenance stamp is never written by the migration; filtering on it "
						 "excludes every row and reports a structural zero")

	def test_no_raw_sql_anywhere_in_the_tool(self):
		for module in (batch, totals, compare):
			self.assertNotIn("frappe.db.sql", inspect.getsource(module),
							 f"{module.__name__} reached for raw SQL — use frappe.qb")

	def test_the_shared_counter_scopes_to_one_lead(self):
		slug = next(iter(C.GRAINS), None)
		if not slug:
			self.skipTest("no grain configured")
		grain = C.Grain(slug)
		self.assertEqual(totals.crm_tasks_by_bare_type(grain, "zz-no-such-lead"), {},
						 "a lead filter must scope the count, not widen it")


class TestGracefulAbort(FrappeTestCase):
	def test_the_abort_flag_round_trips(self):
		run_id = "totals-test-abort-probe"
		self.assertFalse(jobs.abort_requested(run_id))
		jobs.request_abort(run_id)
		self.assertTrue(jobs.abort_requested(run_id), "a requested abort must be visible to the job")
		jobs.clear_abort(run_id)
		self.assertFalse(jobs.abort_requested(run_id), "a cleared flag must not abort the NEXT run")
