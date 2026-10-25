"""One spine for every batch (an import, a partner bulk job, a list action): what it assigns is counted per person and told once per wave."""
import contextlib
import time

import frappe
from frappe import _

from tatva_connect.automation.settings import running_bulk_job
from tatva_connect.notifications import deep_link

BULK_JOB = "CRM Bulk Job"
# The one tick, on CRM Lead Import and CRM Bulk Job: ticked, the batch tells nobody; unticked, it tells in waves.
SKIP_FIELD = "skip_notifications"
# The lead's own record of the bulk job that created it, which a summary's link drills to.
LEAD_FIELD = "custom_bulk_job_id"
# A wave ends when its batch has assigned nothing for this long.
QUIET_SECONDS = 60
# Every key expires, so a batch nobody flushes never lingers.
KEEP_SECONDS = 86400

# The batch a journey walk or a list action run holds; absent outside one.
_HELD = "tatva_batch"


def _key(*parts):
	return frappe.cache.make_key(":".join(("tatva-batch", *parts)))


def _run(*calls):
	"""Raw redis commands on site keys in one MULTI/EXEC; frappe's wrapper methods would key and pickle them again."""
	pipe = frappe.cache.pipeline()
	for name, *args in calls:
		getattr(pipe, name)(*args)
	return pipe.execute()


def bulk_ref(job):
	"""A CRM Bulk Job as a batch; None for none."""
	return f"{BULK_JOB}:{job}" if job else None


def current():
	"""The batch that caused the work in hand: the bulk job this process runs, else the one a walk or a run holds."""
	return bulk_ref(running_bulk_job()) or getattr(frappe.local, _HELD, None)


def bulk_job():
	"""The CRM Bulk Job behind the work in hand, the value a journey and a lead keep; None outside one."""
	ref = current() or ""
	return ref.partition(":")[2] if ref.startswith(f"{BULK_JOB}:") else None


@contextlib.contextmanager
def holding(ref, doctype=None):
	"""Hold `ref` for one journey walk or one list action run; `doctype` names the records a held run assigns."""
	if ref and doctype:
		_run(("hset", _key("meta", ref), "doctype", doctype), ("hset", _key("meta", ref), "by", frappe.session.user),
		     ("expire", _key("meta", ref), KEEP_SECONDS))
	outer = getattr(frappe.local, _HELD, None)
	setattr(frappe.local, _HELD, ref)
	try:
		yield
	finally:
		setattr(frappe.local, _HELD, outer)  # restored, not deleted, so a walk inside a run leaves the outer one intact


def silent():
	"""True while the work in hand belongs to a batch that ticked Don't Notify Assignees."""
	job = bulk_job()
	return bool(job) and bool(frappe.get_cached_value(BULK_JOB, job, SKIP_FIELD))


def covers() -> bool:
	"""Whether the work in hand's assignment notices are its batch's to tell (or to drop, when the batch is silent); the switch does not decide it, so a batch never floods."""
	return bool(current())


def count(user):
	"""Add one assignment `user` received to its batch's next wave; nothing when no batch covers it or the batch is silent."""
	if not covers() or silent():
		return
	ref = current()
	_run(("hincrby", _key("tally", ref), user, 1), ("expire", _key("tally", ref), KEEP_SECONDS),
	     ("setex", _key("last", ref), KEEP_SECONDS, time.time()), ("sadd", _key("open"), ref))


def stamp_lead(doc, method=None):
	"""CRM Lead before_insert: the lead keeps the bulk job that created it."""
	if not doc.get(LEAD_FIELD) and (job := bulk_job()):
		doc.set(LEAD_FIELD, job)


def flush():
	"""Every tick: each batch quiet for QUIET_SECONDS tells each person once, then starts its next wave empty."""
	now = time.time()
	for ref in (r.decode() for r in _run(("smembers", _key("open")))[0]):
		last = _run(("get", _key("last", ref)))[0]
		if last and now - float(last) < QUIET_SECONDS:
			continue
		# Take and clear in one MULTI/EXEC, so a count arriving now opens the next wave and a second flusher gets nothing.
		tally = _run(("hgetall", _key("tally", ref)), ("delete", _key("tally", ref), _key("last", ref)), ("srem", _key("open"), ref))[0]
		if tally:
			_tell(ref, {user.decode(): int(n) for user, n in tally.items()})


def _tell(ref, tally):
	"""One Assignment notice per person for this wave, opening the list of what they hold."""
	from frappe.desk.doctype.notification_log.notification_log import enqueue_create_notification

	kind, name = ref.split(":", 1)
	if kind == BULK_JOB:
		doctype, by = "CRM Lead", frappe.db.get_value(BULK_JOB, name, "partner")
	else:
		meta = {k.decode(): v.decode() for k, v in _run(("hgetall", _key("meta", ref)))[0].items()}
		doctype, by = meta.get("doctype"), meta.get("by")
	if not doctype:
		return  # the run's own record of what it assigned expired before its wave was told
	label = _(doctype.replace("CRM ", "").replace("HD ", "")).lower()
	for user, count in tally.items():
		noun = label if count == 1 else label + "s"
		if user == by:
			continue  # as frappe's notify_assignment: never tell yourself
		drill = {"_assign": ["like", f"%{user}%"]}
		if kind == BULK_JOB:
			drill[LEAD_FIELD] = name
		subject = _("{0} {1} from an import assigned to you") if kind == BULK_JOB else _("{0} {1} assigned to you")
		enqueue_create_notification(user, {
			"type": "Assignment", "subject": subject.format(count, noun), "from_user": by,
			"document_type": doctype, "link": deep_link.list_url(doctype, drill),
		})
