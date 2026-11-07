"""Tatva Analytics: a job runs on its own cadence, a refresh flags calls by the dial-audit rules, and a drifted model fails the migrate.

Run:
    bench --site dev.localhost run-tests --app tatva_connect --module tatva_connect.tests.tatva_analytics.test_analytics_refresh
"""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase
from frappe.utils import add_to_date, now_datetime

from tatva_connect.tatva_analytics import engine, models
from tatva_connect.tatva_analytics.models import call_fact, call_summary

NUMBER = "+919000000401"


def call(minutes_ago, **values):
	doc = frappe.get_doc({"doctype": "CRM Call Log", "id": frappe.generate_hash(length=12), "type": "Outgoing",
	                      "status": "No Answer", "from": "9000000001", "to": NUMBER, "telephony_medium": "Acefone",
	                      "caller": "Administrator", "start_time": add_to_date(now_datetime(), minutes=-minutes_ago), **values})
	return doc.insert(ignore_permissions=True).name


class TestCadence(UnitTestCase):
	def test_a_job_every_3_hours_waits_for_its_next_slot(self):
		row = frappe._dict(frequency="Every 3 hours", last_run_at=datetime(2026, 11, 2, 9, 0, 5))
		self.assertFalse(engine.is_due(row, now=datetime(2026, 11, 2, 11, 0, 5)))
		self.assertTrue(engine.is_due(row, now=datetime(2026, 11, 2, 12, 0, 5)))

	def test_a_cron_job_runs_only_in_its_hours(self):
		row = frappe._dict(frequency="Cron", cron_format="0 9-21/3 * * 1-6", last_run_at=datetime(2026, 11, 2, 21, 0, 5))
		self.assertFalse(engine.is_due(row, now=datetime(2026, 11, 3, 6, 0, 5)))
		self.assertTrue(engine.is_due(row, now=datetime(2026, 11, 3, 9, 0, 5)))


class TestJobRules(IntegrationTestCase):
	def test_a_window_over_the_limit_or_a_summary_with_its_own_frequency_is_refused(self):
		fact = frappe.get_doc(engine.CONTROL, call_fact.JOB)
		fact.window_days = engine.MAX_WINDOW_DAYS + 1
		self.assertRaisesRegex(frappe.ValidationError, "Window", fact.save)
		summary = frappe.get_doc(engine.CONTROL, call_summary.JOB)
		summary.update({"after_job": None, "frequency": "Hourly"})
		self.assertRaisesRegex(frappe.ValidationError, "always runs after", summary.save)


class TestCallFactRefresh(IntegrationTestCase):
	def test_a_refresh_flags_a_flash_dial_its_redial_and_an_own_phone_miss(self):
		flash = call(30, custom_customer_dialled="Yes", custom_customer_ring_sec=1, custom_hangup_reason="cancel")
		redial = call(28, custom_customer_dialled="Yes", custom_customer_ring_sec=25, custom_hangup_reason="noanswer")
		ghost = call(20, custom_customer_dialled="No")

		call_fact.refresh(frappe._dict(started=now_datetime(), watermark=add_to_date(now_datetime(), hours=-1),
		                               window_days=call_fact.WINDOW_DAYS, write=engine.write, checkpoint=lambda: None))

		rows = {r.call_log: r for r in frappe.get_all(call_fact.TARGET, filters={"call_log": ("in", [flash, redial, ghost])},
		                                              fields=["call_log", "f_flash", "f_redial", "f_ghost", "valid", "why_invalid"])}
		self.assertEqual((rows[flash].f_flash, rows[flash].valid, rows[flash].why_invalid), (1, 0, "Flash dial"))
		self.assertEqual((rows[redial].f_redial, rows[redial].valid), (1, 0))
		self.assertEqual((rows[ghost].f_ghost, rows[ghost].why_invalid), (1, "Own phone not picked"))


	def test_a_recording_the_sweep_stores_later_clears_no_recording(self):
		start = now_datetime()

		def refresh(since, at):
			call_fact.refresh(frappe._dict(started=at, watermark=since, window_days=call_fact.WINDOW_DAYS,
			                               write=engine.write, checkpoint=lambda: None))

		with self.freeze_time(start):
			answered = call(40, status="Completed", duration=60, custom_customer_dialled="Yes", custom_customer_ring_sec=8)
			refresh(add_to_date(start, hours=-1), start)
		self.assertEqual(frappe.db.get_value(call_fact.TARGET, {"call_log": answered}, "f_norec"), 1)
		with self.freeze_time(add_to_date(start, minutes=30)):
			frappe.get_doc({"doctype": "CRM Call Media", "call": answered, "recording_state": "Stored"}).insert(ignore_permissions=True)
		refresh(add_to_date(start, minutes=10), add_to_date(start, minutes=40))
		self.assertEqual(frappe.db.get_value(call_fact.TARGET, {"call_log": answered}, "f_norec"), 0)


class TestCallFactKeepsHistory(IntegrationTestCase):
	def run_job(self, model, started, watermark=None):
		return model.refresh(frappe._dict(started=started, watermark=watermark, window_days=model.WINDOW_DAYS,
		                                  write=engine.write, checkpoint=lambda: None))

	def test_a_deleted_call_leaves_and_each_day_keeps_its_summary(self):
		gone = call(30, custom_customer_dialled="Yes", custom_customer_ring_sec=25, custom_hangup_reason="noanswer")
		kept = call(20, custom_customer_dialled="Yes", custom_customer_ring_sec=25, custom_hangup_reason="noanswer")
		first = now_datetime()
		self.run_job(call_fact, first)
		self.run_job(call_summary, first)
		frappe.delete_doc("CRM Call Log", gone, force=True, ignore_permissions=True)
		self.run_job(call_fact, first + timedelta(seconds=1), watermark=first - timedelta(minutes=1))
		self.assertFalse(frappe.db.exists(call_fact.TARGET, {"call_log": gone}), "a deleted call takes its row with it")
		self.assertTrue(frappe.db.exists(call_fact.TARGET, {"call_log": kept}))
		self.run_job(call_summary, first + timedelta(minutes=5))  # the same day updates its snapshot
		self.run_job(call_summary, first + timedelta(days=1))
		days = frappe.get_all(call_summary.TARGET, filters={"agent_user": "Administrator"}, fields=["snapshot", "is_latest"], order_by="snapshot")
		self.assertEqual([(str(d.snapshot), d.is_latest) for d in days],
		                 [(str(first.date()), 0), (str((first + timedelta(days=1)).date()), 1)])


class TestModelLock(IntegrationTestCase):
	def test_the_shipped_models_match_their_tables(self):
		engine.assert_models()

	def test_a_model_whose_query_drifts_from_its_table_fails_the_migrate(self):
		drifted = SimpleNamespace(JOB=call_fact.JOB, TARGET=call_fact.TARGET, WINDOW_DAYS=call_fact.WINDOW_DAYS,
		                          select_sql=lambda: f"SELECT d.*, 1 AS planted FROM ({call_fact.select_sql()}) d")
		# Every other job stays as shipped, so only the planted drift can fail the lock.
		shipped = tuple(drifted if model is call_fact else model for model in models.MODELS)
		with patch.object(models, "MODELS", shipped), self.assertRaisesRegex(frappe.ValidationError, "planted"):
			engine.assert_models()
