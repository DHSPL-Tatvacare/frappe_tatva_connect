"""Meta conversions: a Facebook lead's stage changes, sent back to Meta for the Conversion Leads goal.

Nothing is added to a CRM Lead save. `CRM Lead` keeps `track_changes`, so frappe already writes every stage
change to `tabVersion`; a scheduled pass reads that log from the dataset's `read_upto` and posts what it
finds to `/{dataset_id}/events`. Each batch is one Integration Request row, service `Meta Conversions`.
"""
from zoneinfo import ZoneInfo

import frappe
from frappe.integrations.utils import create_request_log
from frappe.query_builder.functions import IfNull
from frappe.utils import add_days, get_datetime, get_system_timezone, now_datetime

from tatva_connect import automation
from tatva_connect.lead.doctype.crm_facebook_app.crm_facebook_app import _BASE as GRAPH_BASE
from tatva_connect.lead_sync.graph import graph_post
from tatva_connect.utils import book_drain, hold_drain, release_drain, rollback

SWITCH = "Lead::Meta::conversions"
SERVICE = "Meta Conversions"
DATASET = "CRM Meta Dataset"
BATCH = "Batch"  # request_description of a scheduled send
TEST_EVENT = "Test event"  # request_description of a Send Test Event row, so it never counts as a real send
STAGE_FIELD = "custom_substage"
MAX_AGE_DAYS = 7  # Meta refuses a whole batch that holds an event older than this
BATCH_SIZE = 200  # Meta takes 1,000; a refused batch loses less when it is small
READ_LIMIT = 5000  # change-log rows one pass reads; the rest wait for the next pass
LEASE_SECONDS = 300
META_INVALID_PARAMETER = 100  # Meta's code for a batch it refuses on its content
META_NO_ACCESS = 33  # its subcode when the dataset is missing or out of the token's reach
_LOG_LIMIT = 10000  # an Integration Request records the shape of an answer, never an unbounded body


def send_every_15_minutes():
	_send_due("Every 15 Minutes")


def send_hourly():
	_send_due("Hourly")


def send_daily():
	_send_due("Daily")


def _send_due(frequency):
	"""The scheduler entry: every enabled dataset on this frequency, one lease each so passes never overlap."""
	if not automation.is_enabled(SWITCH):
		return
	for name in frappe.get_all(DATASET, filters={"enabled": 1, "send_frequency": frequency}, pluck="name"):
		key = f"meta-conversions::{name}"
		if not book_drain(key, LEASE_SECONDS):
			continue  # the previous pass for this dataset is still running
		try:
			send_pass(frappe.get_doc(DATASET, name), key)
		except Exception:
			rollback()
			frappe.log_error(title=f"meta conversions: pass failed for {name}", message=frappe.get_traceback())
		finally:
			release_drain(key)


def send_pass(dataset, lease_key=None):
	"""Read the stage changes since `read_upto`, send the mapped ones in batches, and move `read_upto` past each."""
	end = now_datetime()
	rows = stage_changes(dataset.read_upto, end)
	if len(rows) == READ_LIMIT:
		end = rows[-1].creation  # a full read stops here, so the next pass starts where this one stopped

	events, stale = mapped_events(dataset, rows)
	if stale:
		frappe.log_error(title=f"meta conversions: {stale} stage changes older than {MAX_AGE_DAYS} days dropped for {dataset.name}")

	for start in range(0, len(events), BATCH_SIZE):
		batch = events[start:start + BATCH_SIZE]
		done, _log = post(dataset, [body for _at, body in batch], BATCH)
		if not done:
			return  # an outage, a dead token or throttling: read_upto stays, so the next pass sends this batch again
		_advance(dataset, batch[-1][0])
		if lease_key:
			hold_drain(lease_key, LEASE_SECONDS)
	_advance(dataset, end)


def mapped_events(dataset, rows):
	"""The rows this dataset's grid maps, as (time, Meta event) oldest first, and how many were too old to send."""
	stage_map = {row.crm_stage: row.meta_event for row in dataset.stages}
	fresh_after = add_days(now_datetime(), -MAX_AGE_DAYS)
	events, stale = [], 0
	for row in rows:
		meta_event = stage_map.get(new_stage(row))
		if not meta_event or not str(row.facebook_lead_id).isdigit():
			continue
		if row.creation < fresh_after:
			stale += 1
			continue
		events.append((row.creation, payload(dataset, row.creation, row.facebook_lead_id, meta_event, row.name)))
	return events, stale


def stage_changes(start, end, limit=READ_LIMIT):
	"""Stage changes on Facebook leads in (start, end], oldest first, read off the change log frappe already keeps."""
	version, lead = frappe.qb.DocType("Version"), frappe.qb.DocType("CRM Lead")
	query = (
		frappe.qb.from_(version)
		.join(lead).on(lead.name == version.docname)
		.select(version.name, version.creation, version.data, lead.facebook_lead_id, lead.custom_current_program)
		.where(version.ref_doctype == "CRM Lead")
		.where(version.creation <= end)
		.where(version.data.like(f'%"{STAGE_FIELD}"%'))
		.where(IfNull(lead.facebook_lead_id, "") != "")
		.orderby(version.creation)
		.orderby(version.name)
		.limit(limit)
	)
	if start:
		query = query.where(version.creation > start)
	return query.run(as_dict=True)


def new_stage(row):
	"""The stage a change-log row moved the lead to, as a CRM Lead Stage name, or None."""
	for change in (frappe.parse_json(row.data) or {}).get("changed") or []:
		if change[0] == STAGE_FIELD and change[2]:
			# Older rows hold the bare label, so it is keyed to the lead's own program the way a stage name is.
			return change[2] if "::" in change[2] else f"{row.custom_current_program}::{change[2]}"
	return None


def payload(dataset, at, lead_id, meta_event, event_id) -> dict:
	"""One event in Meta's Conversion Leads shape. The lead ID is the only person data that leaves."""
	return {
		"event_name": meta_event,
		"event_time": unix_time(at),
		# The change-log row's name, so a batch sent twice is one event to Meta.
		"event_id": event_id,
		"action_source": "system_generated",
		"user_data": {"lead_id": int(lead_id)},
		"custom_data": {"lead_event_source": dataset.lead_event_source, "event_source": "crm"},
	}


def unix_time(site_datetime) -> int:
	"""A site-clock Datetime as Unix seconds. frappe's `get_timestamp` goes through `getdate` and drops the time."""
	return int(get_datetime(site_datetime).replace(tzinfo=ZoneInfo(get_system_timezone())).timestamp())


def post(dataset, events, description):
	"""One batch to Meta, logged as one Integration Request. Returns (done, log); done is False when a resend could still succeed."""
	data = {"data": frappe.as_json(events)}
	if dataset.test_event_code:
		data["test_event_code"] = dataset.test_event_code
	url = f"{GRAPH_BASE}/{dataset.graph_api_version}/{dataset.dataset_id}/events"
	log = create_request_log(
		data, service_name=SERVICE, is_remote_request=1, url=url, request_description=description,
		reference_doctype=DATASET, reference_docname=dataset.name,
	)
	try:
		reply = graph_post("conversions send", url, data, dataset.get_password("access_token"))
	except frappe.ValidationError as failed:  # graph._call throws Meta's reason; it is surfaced on the log row
		log.db_set({"status": "Failed", "error": str(failed)[:_LOG_LIMIT]}, update_modified=False)
		frappe.db.commit()
		return _refused_for_content(frappe.flags.integration_request), log
	log.db_set({"status": "Completed", "output": frappe.as_json(reply)[:_LOG_LIMIT]}, update_modified=False)
	frappe.db.commit()
	return True, log


def _refused_for_content(response) -> bool:
	"""Whether Meta refused the batch itself (code 100), which a resend would repeat. A dead token, throttling or an outage clears up, so those resend."""
	if response is None or response.status_code != 400:
		return False
	try:
		error = (response.json() or {}).get("error") or {}
	except ValueError:  # a 400 without a JSON body is not Meta judging the content; resend it
		return False
	# Subcode 33 is the same code for a wrong dataset ID or a token that cannot reach it: fixable, so resent.
	return error.get("code") == META_INVALID_PARAMETER and error.get("error_subcode") != META_NO_ACCESS


def has_sent(dataset_name) -> bool:
	"""Whether Meta ever accepted a real batch for this dataset. Send Past Days is only offered before that."""
	return bool(frappe.db.exists("Integration Request", {
		"integration_request_service": SERVICE, "reference_doctype": DATASET,
		"reference_docname": dataset_name, "request_description": BATCH, "status": "Completed",
	}))


def _advance(dataset, upto):
	"""Move the dataset's `read_upto` and commit, so a crash later in the pass never resends what went."""
	frappe.db.set_value(DATASET, dataset.name, "read_upto", upto, update_modified=False)
	dataset.read_upto = upto
	frappe.db.commit()
