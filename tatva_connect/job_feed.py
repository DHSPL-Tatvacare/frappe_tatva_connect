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
from frappe.utils import cint
from frappe.utils.background_jobs import create_job_id, is_job_enqueued

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
# How a stopped job's `error_message` begins: who pressed Stop.
ABORTED = "Stopped by "
KINDS = {"bulk": "CRM List Action Job", "export": "CRM Export Job"}


def _seams():
	"""kind -> its `_result`; imported late because both seams import this module."""
	from tatva_connect import bulk_actions, exports

	return {"bulk": bulk_actions._result, "export": exports._result}


def rq_key(doctype, name) -> str:
	"""The job's id in RQ, given at enqueue, so RQ can be asked about it and told to stop it (separation.py's key style)."""
	return f"{doctype}:{name}"


def abandoned(doctype, row) -> bool:
	"""A live row whose job RQ no longer holds, queued or running, has no worker left: no outcome is ever coming. Read, never written."""
	return row.status in LIVE and not is_job_enqueued(rq_key(doctype, row.name))


def stopped(doc) -> bool:
	"""Stopped by a person: the `Stopped` status, or a row stopped before it existed, which kept `Error` and the ABORTED note."""
	return doc.status == "Stopped" or (doc.error_message or "").startswith(ABORTED)


def _live(doctype, filters) -> list:
	return frappe.get_all(doctype, filters={**filters, "status": ("in", LIVE)}, fields=["name", "status"])


def counts(user) -> dict:
	"""The owner's jobs still owed, and the ones not yet looked at; a live job is unseen by birth."""
	running = unseen = 0
	for doctype in KINDS.values():
		running += sum(not abandoned(doctype, r) for r in _live(doctype, {"owner": user}))
		unseen += frappe.db.count(doctype, {"owner": user, "seen": 0})
	return {"running": running, "unseen": unseen}


def _progress_key(name):
	return f"bulk-job-progress:{name}"


def counted(name) -> dict:
	"""A running job's count from Redis, `{}` once it has none."""
	return frappe.cache.get_value(_progress_key(name)) or {}


def _item(kind, doc) -> dict:
	out = {**_seams()[kind](doc), "kind": kind, "seen": cint(doc.seen)}
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
	for doctype in KINDS.values():
		mine = {"owner": user, "seen": 0, **({"name": job} if job else {})}
		# Never a live row: its worker is writing it (a 1020), and finishing sets it unseen again anyway.
		frappe.db.set_value(doctype, {**mine, "status": ("not in", LIVE)}, "seen", 1, update_modified=False)
		# A live row RQ no longer holds has no worker left (`abandoned`), so nothing else would ever clear it.
		if dead := [r.name for r in _live(doctype, mine) if abandoned(doctype, r)]:
			frappe.db.set_value(doctype, {"name": ("in", dead)}, "seen", 1, update_modified=False)
	frappe.publish_realtime(EVENT, counts(user), user=user, after_commit=True)


def halt(doctype, name):
	"""Stop this job's worker with RQ's own stop command; the worker carries on with the next job, and a job not yet started never claims its row.

	RQ's command, not Frappe's `RQ Job.stop_job`: that one is System Manager only, so a rep's own job could never be stopped."""
	from frappe.utils.background_jobs import get_redis_conn
	from rq.command import send_stop_job_command
	from rq.exceptions import InvalidJobOperation, NoSuchJobError

	# Not running, finished or already gone from RQ: nothing is running to stop, which is the state being asked for.
	with suppress(InvalidJobOperation, NoSuchJobError):
		send_stop_job_command(connection=get_redis_conn(), job_id=create_job_id(rq_key(doctype, name)))


def claim(doctype, name, statuses, values, update_modified=True) -> bool:
	"""Write `values` only while the row is in one of `statuses`; True when it now holds the asked status.

	The one guard between Stop and a worker: each moves the row only from where it expects it, so neither overwrites the other."""
	from tatva_connect.utils import retry_on_deadlock

	def _move():
		frappe.db.set_value(doctype, {"name": name, "status": ("in", statuses)}, values, update_modified=update_modified)
		return frappe.db.get_value(doctype, name, "status") == values["status"]

	# A 1020 means the snapshot is older than the row: the retry rolls back and reads the truth.
	return retry_on_deadlock(_move)


def start(kind, job):
	"""The worker's first write: Queued becomes Started, unless Stop got there first. The row, or None."""
	doctype = KINDS[kind]
	if not claim(doctype, job, ("Queued",), {"status": "Started"}, update_modified=False):
		return None  # stopped before a worker reached it, or deleted
	frappe.db.commit()  # the tab is watching this row's status; a drain that runs for a while must not hide it
	doc = frappe.get_doc(doctype, job)
	publish(kind, doc)
	return doc


def finish(kind, doc, values):
	"""The worker's last write: lands only on a row still Started, so a Stop that came first keeps its row and its one message."""
	if claim(doc.doctype, doc.name, ("Started",), {**values, "status": "Completed", "seen": 0}):  # `modified` is the finish time
		frappe.db.commit()
		doc.reload()
		publish(kind, doc)
	forget(doc.name)


def fail(kind, doc, title):
	"""A drain that raised: roll back what it left open, close a row still Started as Error, log the traceback for the operator."""
	error = frappe.get_traceback(with_context=True)
	frappe.db.rollback()
	if claim(doc.doctype, doc.name, ("Started",), {"status": "Error", "error_message": error, "seen": 0}):
		frappe.db.commit()
		doc.reload()
		publish(kind, doc)
	frappe.log_error(title, error)
	forget(doc.name)


@frappe.whitelist(methods=["POST"])
def abort(job: str):
	"""Stop a live job where it is; what it already did stays done, the rest is left as it was."""
	kind = next((k for k, dt in KINDS.items() if frappe.db.exists(dt, job)), None)
	if not kind:
		frappe.throw(_("No such job."), frappe.DoesNotExistError)
	doctype = KINDS[kind]
	# The row's own gate: both job doctypes are owner-only for reps, so a rep stops their own jobs and a System Manager any.
	frappe.has_permission(doctype, "read", doc=job, throw=True)
	# The stopped row keeps the exact count Redis holds: the one write a stop makes, so it carries the job's last word too.
	done = {COLUMNS.get(key, key): value for key, value in counted(job).items()}
	if claim(doctype, job, LIVE, {**done, "status": "Stopped", "error_message": ABORTED + frappe.session.user, "seen": 1}):
		halt(doctype, job)  # a worker that started meanwhile is killed; one that has not never claims the row
		forget(job)
		publish(kind, frappe.get_doc(doctype, job), after_commit=True)
	return _item(kind, frappe.get_doc(doctype, job))  # an ended job is returned as it ended


def publish(kind, doc, after_commit=False):
	"""Tell the owner's tabs where this job stands. `after_commit` when the row was written inside a request: built then, after its enqueue."""
	if after_commit:
		# The same queue `enqueue_after_commit` uses, so a just-queued job is already in RQ when its message is built.
		return frappe.db.after_commit.add(lambda: publish(kind, doc))
	frappe.publish_realtime(EVENT, {**_item(kind, doc), **counts(doc.owner)}, user=doc.owner)


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
