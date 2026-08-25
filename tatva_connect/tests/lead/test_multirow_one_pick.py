# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The multi-row 'latest row' invariant — ONE row, the same one, in EVERY consumer.

The audit found the "latest multi-row child" decided in three places with three tiebreaks
(detail.py, leads.py — retired 2026-08-10 — and smartview/api.py), so two lab rows sharing one report_date resolved to a
DIFFERENT row per surface — a break of the CLAUDE.md invariant. These tests lock the fix: one shared
rule (multirow.latest_child_row) drives the Python consumers, and the SQL builder orders by the same
keys, so a date tie resolves identically everywhere.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.lead.test_multirow_one_pick
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.lead import detail, multirow


def _row(report_date, creation, name):
	return frappe._dict(report_date=report_date, creation=creation, name=name)


class TestLatestChildRule(FrappeTestCase):
	def test_tie_on_key_breaks_by_creation_newest(self):
		# Same report_date -> the newer creation wins (deterministic, not list order).
		older = _row("2026-01-10", "2026-01-10 09:00:00", "aaa")
		newer = _row("2026-01-10", "2026-01-10 18:00:00", "bbb")
		self.assertIs(multirow.latest_child_row([older, newer], "report_date"), newer)
		self.assertIs(multirow.latest_child_row([newer, older], "report_date"), newer)

	def test_tie_on_key_and_creation_breaks_by_name(self):
		a = _row("2026-01-10", "2026-01-10 09:00:00", "aaa")
		z = _row("2026-01-10", "2026-01-10 09:00:00", "zzz")
		self.assertIs(multirow.latest_child_row([a, z], "report_date"), z)
		self.assertIs(multirow.latest_child_row([z, a], "report_date"), z)

	def test_newest_key_wins_over_tiebreak(self):
		# A later report_date beats an earlier one regardless of creation/name.
		early = _row("2026-01-10", "2026-12-31 23:59:59", "zzz")
		late = _row("2026-06-01", "2026-01-01 00:00:00", "aaa")
		self.assertIs(multirow.latest_child_row([early, late], "report_date"), late)

	def test_empty_is_none(self):
		self.assertIsNone(multirow.latest_child_row([], "report_date"))


class TestConsumersAgreeOnTie(FrappeTestCase):
	"""The Data tab must pick the row the SHARED rule picks — it routes through multirow.latest_child_row, so it cannot diverge. (The third consumer, leads.sync_headline_metrics, was retired 2026-08-10; the rule it shared is unchanged.)"""

	def test_data_tab_and_the_shared_rule_pick_the_same_lab_row(self):
		section = frappe.get_cached_doc("CRM Lead Section", "lab")
		key = section.row_key_field
		a = _row("2026-03-01", "2026-03-01 08:00:00", "rowA")
		b = _row("2026-03-01", "2026-03-01 20:00:00", "rowB")  # newer creation, same date
		# Make the child rows carry the section's real row_key_field name (not the literal 'report_date').
		rows = [frappe._dict({key: r.report_date, "creation": r.creation, "name": r.name}) for r in (a, b)]
		doc = frappe._dict({section.child_table_field: rows})

		data_tab_pick = detail._child_row(doc, section)
		shared_pick = multirow.latest_child_row(rows, key)
		self.assertIsNotNone(data_tab_pick)
		self.assertEqual(data_tab_pick.name, shared_pick.name)
		self.assertEqual(data_tab_pick.name, "rowB")  # the shared rule: newest creation on a tie


class TestSmartviewOrderMatches(FrappeTestCase):
	"""The Smart View SQL must order child rows by the SAME keys the Python rule uses: row_key DESC,
	creation DESC, name DESC — so what it reads equals what the other consumers read.

	Asserted against the builder the composer actually calls, never against a window this test wrote
	itself: a test that ranks its own rows proves the test can rank rows, and nothing about production."""

	def test_the_child_window_orders_by_key_creation_name_desc(self):
		from frappe.query_builder import DocType

		from tatva_connect.smartview import api

		order_field = frappe.get_cached_doc("CRM Lead Section", "lab").row_key_field
		inner = DocType("CRM Lab Profile").as_("_tc_src")
		window = api._current_column(inner, "CRM Lab Profile", "hba1c", order_field)
		sql = frappe.qb.from_(inner).select(window.as_("v")).get_sql().lower()
		for field in multirow.order_keys(order_field):
			self.assertIn(f"`{field}` desc", sql)
		self.assertRegex(sql, r"order by.*desc")
