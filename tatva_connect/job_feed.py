# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The Bulk Actions panel's one feed: a person's list-action and export jobs, read the way the notification tray reads Notification Log.

ONE PATH. The row is the job's durable record, written only when it is queued, starts, ends or is stopped, and each of those
writes publishes `EVENT`. A running job's count is not in the row: it is one Redis value per job (`frappe.cache`), overwritten
after every record and expiring on its own, as RQ and Celery keep progress. The tabs hear it at most once a `SAY`; the feed, a
refresh, a second tab and Stop read it from Redis. The panel reads `feed` when it opens; there is no poll.
"""
import time
from contextlib import suppress

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, now_datetime, time_diff_in_seconds

EVENT = "crm_bulk_job"
# Rows per page, the panel's Load more step.
PAGE = 20
# Seconds between two live messages, and between two saves of the running count, for one job.
SAY = 1
# How long a running job's count outlives its last write in Redis: past the longest job timeout, so a live job never loses it.
PROGRESS_TTL = 3600
LIVE = ("Queued", "Started")
# A count's name in Redis and the panel -> its column on the job row, where the two differ.
COLUMNS = {"rows": "row_count"}
# How a stopped job's `error_message` begins, so its row reads "Stopped" and never a failure.
ABORTED = "Stopped by "
KINDS = {"bulk": "CRM List Action Job", "export": "CRM Export Job"}


def _seams():
	"""kind -> (its `_result`, its enqueue timeout); imported late because both seams import this module."""
	from tatva_connect import bulk_actions, exports
	from tatva_connect.tatva_connect.doctype.crm_export_job.crm_export_job import EXPORT_TIMEOUT
	from tatva_connect.tatva_connect.doctype.crm_list_action_job.crm_list_action_job import BULK_TIMEOUT

	return {"bulk": (bulk_actions._result, BULK_TIMEOUT), "export": (exports._result, EXPORT_TIMEOUT)}


def abandoned(doc, timeout) -> bool:
	"""Has this job outlived the timeout RQ was given? Then no outcome is ever coming. Read, never written."""
	return doc.status in LIVE and time_diff_in_seconds(now_datetime(), doc.creation) > timeout


def counts(user) -> dict:
	"""The owner's jobs still owed, and the ones not yet looked at; a live job is unseen by birth."""
	running = unseen = 0
	for kind, (_result, timeout) in _seams().items():
		doctype, since = KINDS[kind], add_to_date(now_datetime(), seconds=-timeout)
		running += frappe.db.count(doctype, {"owner": user, "status": ("in", LIVE), "creation": (">", since)})
		unseen += frappe.db.count(doctype, {"owner": user, "seen": 0})
	return {"running": running, "unseen": unseen}


def _progress_key(name):
	return f"bulk-job-progress:{name}"


def counted(name) -> dict:
	"""A running job's count from Redis, `{}` once it has none."""
	return frappe.cache.get_value(_progress_key(name)) or {}


def _item(kind, doc) -> dict:
	result, _timeout = _seams()[kind]
	out = {**result(doc), "kind": kind, "seen": cint(doc.seen)}
	return {**out, **counted(doc.name)} if doc.status in LIVE else out


@frappe.whitelist()
def feed(limit: int = PAGE):
	"""The caller's own jobs, newest first: one page across both tables, the counts, and whether more exist."""
	limit, user = cint(limit) or PAGE, frappe.session.user
	rows = []
	for kind, doctype in KINDS.items():
		# One read per table, rows and all; one row past the page tells whether more exist, without a COUNT.
		found = frappe.get_list(doctype, filters={"owner": user}, fields=["*"], order_by="creation desc", limit=limit + 1)
		rows += [(kind, r) for r in found]
	rows.sort(key=lambda r: r[1].creation, reverse=True)
	items = [_item(kind, r) for kind, r in rows[:limit]]
	return {"items": items, "has_more": len(rows) > limit, **counts(user)}


@frappe.whitelist(methods=["POST"])
def mark_seen(job: str | None = None):
	"""Mark the caller's own finished job, or all of them, as looked at; every open tab hears the new counts."""
	user = frappe.session.user
	for kind, (_result, timeout) in _seams().items():
		mine = {"owner": user, "seen": 0, **({"name": job} if job else {})}
		# Never a live row: its worker is writing it (a 1020), and finishing sets it unseen again anyway.
		frappe.db.set_value(KINDS[kind], {**mine, "status": ("not in", LIVE)}, "seen", 1, update_modified=False)
		# A row still Queued or Started past its own timeout has no worker left (`abandoned`), so nothing else would ever clear it.
		dead = {**mine, "status": ("in", LIVE), "creation": ("<", add_to_date(now_datetime(), seconds=-timeout))}
		frappe.db.set_value(KINDS[kind], dead, "seen", 1, update_modified=False)
	frappe.publish_realtime(EVENT, counts(user), user=user, after_commit=True)


def halt(doc):
	"""Stop this job with RQ's own stop command; its worker carries on with the next job, and a job not yet started is skipped by its `run`.

	RQ's command, not Frappe's `RQ Job.stop_job`: that one is System Manager only, so a rep's own job could never be stopped."""
	from frappe.utils.background_jobs import get_redis_conn
	from rq.command import send_stop_job_command
	from rq.exceptions import InvalidJobOperation, NoSuchJobError

	if doc.status != "Started" or not doc.job_id:
		return
	# Finished or already gone from RQ: nothing is running to stop, which is the state being asked for.
	with suppress(InvalidJobOperation, NoSuchJobError):
		send_stop_job_command(connection=get_redis_conn(), job_id=doc.job_id)


@frappe.whitelist(methods=["POST"])
def abort(job: str):
	"""Stop a live job where it is; what it already did stays done, the rest is left as it was."""
	from tatva_connect.utils import retry_on_deadlock

	kind = next((k for k, dt in KINDS.items() if frappe.db.exists(dt, job)), None)
	if not kind:
		frappe.throw(_("No such job."), frappe.DoesNotExistError)
	# The row's own gate: both job doctypes are owner-only for reps, so a rep stops their own jobs and a System Manager any.
	frappe.has_permission(KINDS[kind], "read", doc=job, throw=True)
	doc = frappe.get_doc(KINDS[kind], job)
	if doc.status not in LIVE:
		return _item(kind, doc)  # it ended while the person was deciding
	halt(doc)
	# The stopped row keeps the exact count Redis holds: the one write a stop makes, so it carries the job's last word too.
	done = {COLUMNS.get(key, key): value for key, value in counted(doc.name).items()}
	retry_on_deadlock(lambda: doc.db_set({**done, "status": "Error", "error_message": ABORTED + frappe.session.user, "seen": 1}))
	frappe.cache.delete_value(_progress_key(doc.name))
	publish(kind, doc, after_commit=True)
	return _item(kind, doc)


def publish(kind, doc, after_commit=False):
	"""Tell the owner's tabs where this job stands. `after_commit` when the row was written inside a request."""
	frappe.publish_realtime(EVENT, {**_item(kind, doc), **counts(doc.owner)}, user=doc.owner, after_commit=after_commit)


def progress(kind, doc):
	"""A running job's count: overwritten in Redis after every record, and told to the owner's tabs at most once a `SAY`."""
	said = 0.0

	def report(counts):
		nonlocal said
		frappe.cache.set_value(_progress_key(doc.name), counts, expires_in_sec=PROGRESS_TTL)
		now = time.monotonic()
		if now - said >= SAY:
			said = now
			frappe.publish_realtime(EVENT, {"job": doc.name, "kind": kind, "status": "Started", **counts}, user=doc.owner)

	return report


def forget(name):
	"""Drop a finished job's count: its row now holds the final one."""
	frappe.cache.delete_value(_progress_key(name))
