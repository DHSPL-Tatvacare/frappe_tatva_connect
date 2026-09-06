"""The silence sweep's invariants: it ships dormant, it costs nothing until armed, and its window matches the cadence hooks.py runs it at."""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect import hooks
from tatva_connect.observability import silence


def _settings_json():
	"""The Settings doctype as it ships on disk — the source of truth, and readable before a migrate has run."""
	import json
	import os

	path = os.path.join(os.path.dirname(silence.__file__), "..", "partner_api", "doctype",
	                    "crm_partner_api_settings", "crm_partner_api_settings.json")
	return json.load(open(os.path.normpath(path)))


class TestPartnerSilence(FrappeTestCase):
	def test_the_window_matches_the_cadence_it_is_scheduled_at(self):
		"""The window is one pass wide, so the two must agree: a wider cadence misses a contract entirely, a narrower one tells about it twice."""
		entries = [cron for cron, fns in hooks.scheduler_events["cron"].items() if any(f.endswith("silence.sweep") for f in fns)]
		self.assertEqual(len(entries), 1, "silence.sweep must be scheduled exactly once")
		minute, hour, dom, month, dow = entries[0].split()
		self.assertEqual((hour, dom, month, dow), ("*", "*", "*", "*"), "an hourly cadence, or INTERVAL_MINUTES is wrong")
		self.assertEqual(len(minute.split(",")), 1, "one firing per hour, or INTERVAL_MINUTES is wrong")
		self.assertEqual(silence.INTERVAL_MINUTES, 60)

	def test_it_ships_dormant_and_under_the_logging_switch(self):
		"""Constitution: every automation ships off. And silence is indistinguishable from not-logging, so it hangs off the switch that writes the rows it reads."""
		from tatva_connect.automation import registry

		self.assertEqual(registry.parent_of(silence.SWITCH), "Observability::Requests::logging")
		self.assertFalse(frappe.db.get_value("CRM Tatva Automation", silence.SWITCH, "enabled"), "must ship dormant")

	def test_a_dormant_sweep_reads_nothing(self):
		"""The gate is the first line, before any query — an unarmed alert costs one cached switch read per hour."""
		queries = []
		original = frappe.db.sql
		frappe.db.sql = lambda *a, **k: (queries.append(a[0] if a else ""), original(*a, **k))[1]
		try:
			self.assertIsNone(silence.sweep())
		finally:
			frappe.db.sql = original
		self.assertFalse([q for q in queries if silence.LOG in str(q)], "a dormant sweep must not touch the request log")

	def test_a_missing_or_zero_threshold_falls_back_and_never_reaches_zero(self):
		"""No hand-rolled validator: `non_negative` on the field bars a negative and a blank reads as the default, so the read floor is the only guard the sweep needs."""
		self.assertEqual(max(silence.MIN_HOURS, silence.cint(None) or silence.DEFAULT_HOURS), silence.DEFAULT_HOURS)
		self.assertEqual(max(silence.MIN_HOURS, silence.cint(0) or silence.DEFAULT_HOURS), silence.DEFAULT_HOURS)
		self.assertEqual(max(silence.MIN_HOURS, silence.cint(3) or silence.DEFAULT_HOURS), 3)
		field = next(f for f in _settings_json()["fields"] if f["fieldname"] == "silence_alert_hours")
		self.assertTrue(field.get("non_negative"), "the field itself must bar a negative, so no validator has to")
