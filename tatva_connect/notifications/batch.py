"""One spine for every batch — a CRM Bulk Job (an import, a partner upload), a CRM List Action Job (a queued list action), a Tatva User Separation (a hand-over), a CRM Workflow (a scheduled cohort): what it assigns is counted per person and told once per wave."""
import contextlib
import time

import frappe
from frappe import _

from tatva_connect.automation.settings import running_bulk_job
from tatva_connect.notifications import deep_link

BULK_JOB = "CRM Bulk Job"
# The one tick, on CRM Lead Import and CRM Bulk Job: ticked, the batch tells nobody; unticked, it tells in waves.
SKIP_FIELD = "skip_notifications"
# A record's note of the last bulk action that created, changed or assigned it, which a wave's link drills to.
STAMP = ("custom_batch_doctype", "custom_batch_name")
# A wave ends when its batch has assigned nothing for this long, and never sooner than one workflow drain pass plus this margin.
QUIET_SECONDS = 60
PASS_MARGIN_SECONDS = 15
# A scheduled cohort is run by its schedule, not by a person, so its waves skip nobody.
SCHEDULED = "CRM Workflow"
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


def current():
	"""The batch the work in hand belongs to, as `(batch_doctype, batch_name)`: the bulk job this process runs, else the one a run or a journey walk holds; None outside one."""
	job = running_bulk_job()
	return (BULK_JOB, job) if job else getattr(frappe.local, _HELD, None)


@contextlib.contextmanager
def holding(batch_doctype, batch_name):
	"""Run the block as part of this batch (a list action run, a hand-over, a journey walk); a blank name holds none."""
	outer = getattr(frappe.local, _HELD, None)
	setattr(frappe.local, _HELD, (batch_doctype, batch_name) if batch_name else None)
	try:
		yield
	finally:
		setattr(frappe.local, _HELD, outer)  # restored, not deleted, so a walk inside a run leaves the outer one intact


def silent():
	"""True while the work in hand belongs to a batch that ticked Don't Notify Assignees."""
	held = current()
	return bool(held) and frappe.get_meta(held[0]).has_field(SKIP_FIELD) and bool(frappe.get_cached_value(*held, SKIP_FIELD))


def covers() -> bool:
	"""Whether the work in hand's assignment notices are its batch's to tell (or to drop, when the batch is silent); the switch does not decide it, so a batch never floods."""
	return bool(current())


def count(user, doctype, name):
	"""Note the batch on the record `user` was given, and add it to the batch's next wave unless the batch is silent; nothing outside a batch."""
	held = current()
	if not held:
		return
	if frappe.get_meta(doctype).has_field(STAMP[1]):
		# An assignment saves no document, so the record is noted here; `modified` stays, so a save already in hand is never refused.
		frappe.db.set_value(doctype, name, dict(zip(STAMP, held, strict=True)), update_modified=False)
	if silent():
		return
	wave = frappe.as_json([*held, doctype], indent=None)  # one wave per batch and per kind of record it hands out
	_run(("hincrby", _key("tally", wave), user, 1), ("expire", _key("tally", wave), KEEP_SECONDS),
	     ("setex", _key("last", wave), KEEP_SECONDS, time.time()), ("sadd", _key("open"), wave))


def stamp(doc, method=None):
	"""CRM Lead and CRM Task before_save: inside a batch, the record notes it as the last bulk action that created or changed it."""
	if held := current():
		doc.update(dict(zip(STAMP, held, strict=True)))


def flush():
	"""Every tick: each batch quiet for its window tells each person once, then starts its next wave empty."""
	now, quiet = time.time(), _quiet_seconds()
	for wave in (r.decode() for r in _run(("smembers", _key("open")))[0]):
		last = _run(("get", _key("last", wave)))[0]
		if last and now - float(last) < quiet:
			continue
		# Take and clear in one MULTI/EXEC, so a count arriving now opens the next wave and a second flusher gets nothing.
		tally = _run(("hgetall", _key("tally", wave)), ("delete", _key("tally", wave), _key("last", wave)), ("srem", _key("open"), wave))[0]
		if tally:
			_tell(wave, {user.decode(): int(n) for user, n in tally.items()})


def _quiet_seconds():
	"""How long a wave waits for more: never shorter than the gap between two workflow drain passes, which walk a batch's journeys."""
	from tatva_connect.access.doctype.tatva_platform_settings.tatva_platform_settings import site_pace

	return max(QUIET_SECONDS, site_pace()[1] + PASS_MARGIN_SECONDS)


def _tell(wave, tally):
	"""One Assignment notice per person for this wave, opening the records this batch gave them."""
	from frappe.desk.doctype.notification_log.notification_log import enqueue_create_notification

	batch_doctype, batch_name, doctype = frappe.parse_json(wave)
	if not frappe.db.exists(batch_doctype, batch_name):
		return  # the batch's own record is gone, so nobody is left to say who sent the work
	by = None if batch_doctype == SCHEDULED else frappe.db.get_value(batch_doctype, batch_name, "owner")
	label = _(doctype.replace("CRM ", "").replace("HD ", "")).lower()
	noted = frappe.get_meta(doctype).has_field(STAMP[1])
	for user, count in tally.items():
		noun = label if count == 1 else label + "s"
		if user == by:
			continue  # as frappe's notify_assignment: never tell yourself
		drill = {"_assign": ["like", f"%{user}%"], **(dict(zip(STAMP, (batch_doctype, batch_name), strict=True)) if noted else {})}
		subject = _("{0} {1} from an import assigned to you") if batch_doctype == BULK_JOB else _("{0} {1} assigned to you")
		enqueue_create_notification(user, {
			"type": "Assignment", "subject": subject.format(count, noun), "from_user": by,
			"document_type": doctype, "link": deep_link.list_url(doctype, drill),
		})
