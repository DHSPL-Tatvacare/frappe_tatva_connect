# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Scheduled batch: a native Webhook whose Doc Event is `scheduled_batch` posts ONE body per window, never one per row.

Frappe fires a webhook on a save; this adds a timer and nothing else. A pass reads the rows its DocType created in
the last window, keeps those the webhook's own Condition accepts, and posts once — rendered by the webhook's own
JSON body, signed by its own headers, recorded in its own Webhook Request Log. A failed post is logged and dropped,
never retried. `Send When: No Rows Found` turns the same pass into a silence alarm.
"""
import frappe
import requests
from frappe import _
from frappe.integrations.doctype.webhook.webhook import get_context, get_webhook_headers, log_request
from frappe.utils import add_to_date, now_datetime

from tatva_connect import automation
from tatva_connect.automation.registry import AUTOMATIONS
from tatva_connect.utils import book_drain, release_drain, rollback

SWITCH = "Notify::Webhook::batches"
EVENT = "scheduled_batch"
NO_ROWS_FOUND = "No Rows Found"
# Batch Frequency option -> window in minutes; a test holds these keys to the Custom Field's options.
FREQUENCIES = {
	"Every 5 Minutes": 5,
	"Every 10 Minutes": 10,
	"Every 15 Minutes": 15,
	"Hourly": 60,
	"Every 4 Hours": 240,
	"Daily": 1440,
}
READ_LIMIT = 5000  # rows one window reads; a fuller window says so through `capped`
CARD_ROWS = 10  # a notification names a few rows; the link carries the rest
LEASE_SECONDS = 300


def run_every_5_minutes():
	_run_due("Every 5 Minutes")


def run_every_10_minutes():
	_run_due("Every 10 Minutes")


def run_every_15_minutes():
	_run_due("Every 15 Minutes")


def run_hourly():
	_run_due("Hourly")


def run_every_4_hours():
	_run_due("Every 4 Hours")


def run_daily():
	_run_due("Daily")


def _run_due(frequency):
	"""The scheduler entry: every enabled batch webhook on this frequency, one lease each so passes never overlap."""
	if not automation.is_enabled(SWITCH):
		return
	webhooks = frappe.get_all(
		"Webhook", filters={"enabled": 1, "webhook_docevent": EVENT, "batch_frequency": frequency}, pluck="name"
	)
	for name in webhooks:
		key = f"webhook-batch::{name}"
		token = book_drain(key, LEASE_SECONDS)
		if not token:
			continue  # the previous pass for this webhook is still running
		try:
			run_batch(frappe.get_doc("Webhook", name), FREQUENCIES[frequency])
			frappe.db.commit()  # each webhook's log stands alone: a later webhook's rollback must not erase it
		except Exception:
			rollback()
			frappe.log_error(title=f"webhook batch: pass failed for {name}", message=frappe.get_traceback())
		finally:
			release_drain(key, token)


def run_batch(webhook, minutes):
	"""One window for one webhook: read, keep what the Condition accepts, post once if Send When says so."""
	start, end = _window(minutes)
	rows = frappe.get_all(
		webhook.webhook_doctype,
		filters=[["creation", ">=", start], ["creation", "<", end]],
		fields=["*"],
		order_by="creation desc",
		limit=READ_LIMIT,
	)
	kept = [row for row in rows if _accepts(webhook, row)]
	if bool(kept) == (webhook.send_when == NO_ROWS_FOUND):
		return
	_post(webhook, kept, capped=len(rows) == READ_LIMIT, start=start, end=end)


def _window(minutes):
	"""The last CLOSED slot on the clock, counted from midnight: passes meet edge to edge however late each one runs."""
	now = now_datetime()
	midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
	elapsed = int((now - midnight).total_seconds() // 60)
	end = add_to_date(midnight, minutes=elapsed - elapsed % minutes)
	return add_to_date(end, minutes=-minutes), end


def _accepts(webhook, row):
	"""The webhook's own Condition, evaluated the way frappe's `run_webhooks` evaluates it on a save."""
	return not webhook.condition or frappe.safe_eval(webhook.condition, eval_locals=get_context(row))


def _post(webhook, kept, capped, start, end):
	"""Render, sign and send ONE body, then file it in Webhook Request Log, sent or not."""
	# Dates become strings first, as frappe's own webhook does, so a template can `tojson` any field.
	docs = [frappe._dict(frappe.parse_json(frappe.as_json(row))) for row in kept[:CARD_ROWS]]
	doc = docs[0] if docs else frappe._dict()
	context = {
		**get_context(doc),
		"docs": docs,
		"count": len(kept),
		"capped": capped,
		"window_from": str(start),
		"window_to": str(end),
	}
	data = frappe.parse_json(frappe.render_template(webhook.webhook_json or "{}", context))
	url = frappe.render_template(webhook.request_url, context) if webhook.is_dynamic_url else webhook.request_url
	headers = get_webhook_headers(doc, webhook, data=data)
	res = None
	# `requests`, not make_post_request: the native webhook's own call, and log_request needs the raw response.
	try:
		res = requests.request(
			method=webhook.request_method, url=url, data=frappe.as_json(data), headers=headers, timeout=webhook.timeout or 5
		)
		res.raise_for_status()
	except requests.RequestException:
		# Dropped, never retried (owner, 2026-10-17): logged inside the except, so the row carries the traceback.
		log_request(webhook.name, webhook.webhook_doctype, doc.get("name"), url, headers, data, res)
	else:
		log_request(webhook.name, webhook.webhook_doctype, doc.get("name"), url, headers, data, res)


def validate_batch(doc, method=None):
	"""Webhook validate: a batch body is rendered from its JSON template, so a batch webhook must be a JSON one."""
	if doc.webhook_docevent == EVENT and doc.request_structure != "JSON":
		frappe.throw(_("A scheduled_batch webhook sends one JSON body: set Request Structure to JSON."))


def apply_batches(enabled):
	"""Activator for Notify::Webhook::batches: the six jobs are stopped while it is off, so none is ever enqueued."""
	for method in next(auto.backs for auto in AUTOMATIONS if auto.key == SWITCH):
		name = frappe.db.get_value("Scheduled Job Type", {"method": method}, "name")
		if name:
			frappe.db.set_value("Scheduled Job Type", name, "stopped", 0 if enabled else 1)
