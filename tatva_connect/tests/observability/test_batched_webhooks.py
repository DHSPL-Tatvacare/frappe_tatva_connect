"""Scheduled batch webhooks: a burst posts ONCE, silence posts when nothing matched, windows meet edge to edge, and the form refuses what the engine cannot send."""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, get_datetime, now_datetime

from tatva_connect.observability import batched_webhooks as batches

BODY = '{"count": {{ count }}, "names": {{ (docs or [doc]) | map(attribute="name") | list | tojson }}}'


class _Sent:
	status_code = 202
	text = ""

	def raise_for_status(self):
		return None


def _webhook(token, send_when, structure="JSON"):
	return frappe.get_doc({
		"doctype": "Webhook",
		"__newname": f"batch-{token}",  # Webhook is named by prompt
		"webhook_doctype": "ToDo",
		"webhook_docevent": batches.EVENT,
		"batch_frequency": "Every 5 Minutes",
		"send_when": send_when,
		"condition": f'"{token}" in (doc.description or "")',
		"request_url": "https://alerts.example.invalid/hook",
		"request_method": "POST",
		"request_structure": structure,
		"webhook_json": BODY,
		"enabled": 1,
	}).insert()


def _todos(token, n):
	for i in range(n):
		frappe.get_doc({"doctype": "ToDo", "description": f"{token} {i}"}).insert()


def _at(moment):
	"""Run a pass as if the clock read `moment` — frappe's clock, never our code."""
	return patch.object(batches, "now_datetime", return_value=get_datetime(moment))


def _next_pass():
	"""A pass one window later: the slot it closes holds every row written now."""
	return add_to_date(now_datetime(), minutes=5)


class TestBatchedWebhooks(FrappeTestCase):
	def test_a_burst_posts_once_with_ten_rows_and_the_true_count(self):
		"""25 matching saves reach the endpoint as ONE body, and frappe's per-save path queues nothing for this webhook."""
		token = frappe.generate_hash(length=10)
		hook = _webhook(token, "Rows Found")
		_todos(token, 25)
		queued = [e for e in (getattr(frappe.local, "_webhook_queue", None) or []) if e.webhook.get("name") == hook.name]
		self.assertEqual(queued, [], "a scheduled_batch webhook must never fire on a save")

		with _at(_next_pass()), patch.object(batches.requests, "request", return_value=_Sent()) as post:
			batches.run_batch(hook, 5)

		self.assertEqual(post.call_count, 1)
		sent = frappe.parse_json(post.call_args.kwargs["data"])
		self.assertEqual(sent["count"], 25)
		self.assertEqual(len(sent["names"]), batches.CARD_ROWS)
		self.assertEqual(frappe.db.count("Webhook Request Log", {"webhook": hook.name}), 1)

	def test_no_rows_found_posts_on_silence_and_stays_quiet_once_a_row_arrives(self):
		"""Send When = No Rows Found is the silence alarm: it speaks for an empty window and only then."""
		token = frappe.generate_hash(length=10)
		hook = _webhook(token, batches.NO_ROWS_FOUND)
		at = _next_pass()

		with patch.object(batches.requests, "request", return_value=_Sent()) as post:
			with _at(at):
				batches.run_batch(hook, 5)
			self.assertEqual(post.call_count, 1)
			self.assertEqual(frappe.parse_json(post.call_args.kwargs["data"])["count"], 0)

			_todos(token, 1)
			with _at(at):
				batches.run_batch(hook, 5)
			self.assertEqual(post.call_count, 1, "a matching row in the window must silence the alarm")

	def test_windows_meet_edge_to_edge_however_late_the_pass_runs(self):
		"""A late pass reads the same closed slot as a punctual one, so no row falls between two windows or into both."""
		with _at("2026-10-17 10:06:02"):
			punctual = batches._window(5)
		with _at("2026-10-17 10:09:58"):
			late = batches._window(5)
		with _at("2026-10-17 10:11:30"):
			following = batches._window(5)
		self.assertEqual(punctual, late)
		self.assertEqual(punctual, (get_datetime("2026-10-17 10:00:00"), get_datetime("2026-10-17 10:05:00")))
		self.assertEqual(following[0], punctual[1], "the next window starts exactly where this one ended")

	def test_the_form_offers_only_what_the_engine_runs(self):
		"""Options, windows and jobs are one list with nothing pre-chosen, and a non-JSON batch webhook is refused at save."""
		options = frappe.get_meta("Webhook").get_field("batch_frequency").options.split("\n")
		self.assertEqual(options[0], "", "a blank first option, or frappe pre-fills a frequency nobody chose")
		self.assertEqual(options[1:], list(batches.FREQUENCIES))
		from tatva_connect.automation.registry import AUTOMATIONS

		backs = next(a.backs for a in AUTOMATIONS if a.key == batches.SWITCH)
		self.assertEqual(len([b for b in backs if ".run_" in b]), len(batches.FREQUENCIES))

		with self.assertRaises(frappe.ValidationError):
			_webhook(frappe.generate_hash(length=10), "Rows Found", structure="Form URL-Encoded")
