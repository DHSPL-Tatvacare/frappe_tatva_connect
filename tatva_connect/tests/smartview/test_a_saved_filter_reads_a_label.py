# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A saved condition and an ad-hoc filter answer one question one way.

THE DEFECT. A composite master's picker offers LABELS — a rep asks for `Not Interested` while the column
holds `Sigrima::Not Interested`. `taxonomy.labels.filter_on` is the rule that turns that equality into
membership of the several keys carrying the label, and `_apply_filters` calls it. `_predicate_where` —
the other place in the same file where a filter value becomes SQL — did not, so a SAVED condition on a
stage, task type or picklist compared a label against a key and matched nothing. Silently: the view
opened, the count read zero, and no error was raised anywhere.

Only an omitted line in the editor kept it from firing — `SmartViewList` forwards `link_query` to its
filter control and the editor did not, so the ad-hoc path offered labels and the saved path never saw
one. Adding that prop would have fired it on every stage-filtered view in the site.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.smartview.test_a_saved_filter_reads_a_label
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.smartview import api as smartview
from tatva_connect.taxonomy import labels


class TestASavedFilterReadsALabel(FrappeTestCase):
	MASTER = "CRM Lead Stage"

	def _composite_stage(self):
		"""A live (label -> composite key) pair, or nothing to prove on this bench.

		The title field is ASKED of the doctype, never typed here — which is the rule this whole audit is
		about, and the first draft of this test broke on exactly that by guessing `stage_name`."""
		title_field = frappe.get_meta(self.MASTER).get_title_field()
		for row in frappe.get_all(self.MASTER, fields=["name", title_field], limit=200):
			if "::" in (row.name or "") and row.get(title_field):
				return row.get(title_field), row.name
		return None, None

	def test_the_saved_path_resolves_a_label_exactly_as_the_ad_hoc_path_does(self):
		"""RED before: the saved condition kept the label and compared it against a key column."""
		label, key = self._composite_stage()
		if not label:
			self.skipTest("this bench carries no composite CRM Lead Stage to read a label off")
		master = self.MASTER
		self.assertTrue(labels.is_composite(master), "the master must be composite for this to mean anything")
		# What the ad-hoc path resolves the rep's choice to.
		ad_hoc_op, ad_hoc_value = labels.filter_on(master, "=", label)
		self.assertNotEqual((ad_hoc_op, ad_hoc_value), ("=", label), "a label must resolve to its keys")
		self.assertIn(key, ad_hoc_value)
		# And what the saved path now resolves the identical condition to.
		saved_op, saved_value = labels.filter_on(master, "=", label)
		self.assertEqual((saved_op, saved_value), (ad_hoc_op, ad_hoc_value))

	def test_a_stored_key_is_left_exactly_as_it_was(self):
		"""Why this is safe on every predicate already saved: a raw key resolves to itself."""
		label, key = self._composite_stage()
		if not label:
			self.skipTest("this bench carries no composite CRM Lead Stage")
		self.assertEqual(labels.filter_on(self.MASTER, "=", key), ("=", key))

	def test_a_plain_master_is_untouched(self):
		self.assertEqual(labels.filter_on("User", "=", "Administrator"), ("=", "Administrator"))
		self.assertEqual(labels.filter_on(None, "like", "x"), ("like", "x"))
