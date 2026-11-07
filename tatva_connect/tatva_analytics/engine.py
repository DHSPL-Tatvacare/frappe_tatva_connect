"""Tatva Analytics: run each enabled job on its own cadence from what changed since its watermark. A job is one module in models/."""
import time
from datetime import datetime

import frappe
from frappe import _
from frappe.core.doctype.scheduled_job_type.scheduled_job_type import parse_cron
from frappe.utils import get_datetime, now_datetime
from frappe.utils.background_jobs import is_job_enqueued
from pypika.terms import Values

from tatva_connect import automation
from tatva_connect.tatva_analytics import models

KEY = "Analytics::Pipeline::runs"
CONTROL = "Tatva Analytics Control"
RUN_LOG = "Tatva Analytics Run Log"
RUN_LOG_DAYS = 30
CHUNK = 1000
# No job ever loads more than this many days: a wider window would stall the live database.
MAX_WINDOW_DAYS = 30
# A named frequency is a cron string, as in Frappe's own Scheduled Job Type.
FREQUENCIES = {"Hourly": "0 * * * *", "Every 3 hours": "0 */3 * * *", "Every 6 hours": "0 */6 * * *",
               "Every 12 hours": "0 */12 * * *", "Daily": "0 0 * * *"}


def window_limit(model):
	return min(getattr(model, "MAX_WINDOW_DAYS", MAX_WINDOW_DAYS), MAX_WINDOW_DAYS)


def window_days(model, control):
	return min(control.window_days or model.WINDOW_DAYS, window_limit(model))


def dispatch():
	"""hourly_long: queue every enabled job whose cadence is due."""
	if not automation.is_enabled(KEY):
		return
	rows = frappe.get_all(CONTROL, filters={"enabled": 1, "after_job": ("is", "not set")},
	                      fields=["name", "frequency", "cron_format", "last_run_at", "creation"])
	for row in rows:
		if is_due(row):
			queue(row.name, "Schedule")


def is_due(row, now=None):
	cron = row.cron_format if row.frequency == "Cron" else FREQUENCIES.get(row.frequency)
	if not cron:
		return False
	return parse_cron(cron).get_next(datetime, start_time=get_datetime(row.last_run_at or row.creation)) <= (now or now_datetime())


def job_id(job):
	return f"tatva-analytics::{job}"


def queue(job, triggered_by):
	frappe.enqueue(f"{__name__}.run", queue="long", job_id=job_id(job), deduplicate=True, job=job, triggered_by=triggered_by)


@frappe.whitelist(methods=["POST"])
def run_now(job: str):
	queue(_startable(job), "Manual")


@frappe.whitelist(methods=["POST"])
def rebuild(job: str):
	"""Reload this job's window from scratch; the watermark is cleared by the run itself, so a refused rebuild changes nothing."""
	queue(_startable(job), "Rebuild")


def _startable(job):
	frappe.has_permission(CONTROL, "write", doc=job, throw=True)
	automation.require(KEY, _("The analytics pipeline"))
	if not frappe.db.get_value(CONTROL, job, "enabled"):
		frappe.throw(_("Turn this job on first."))
	if is_job_enqueued(job_id(job)):
		frappe.throw(_("This job is already queued or running. Its run appears in the Run Log."))
	return job


def run(job, triggered_by):
	"""One run of one job: log it Running, refresh its table, record the outcome, then queue the jobs that run after it."""
	model, control = models.by_job()[job], frappe.get_doc(CONTROL, job)
	started, clock = now_datetime(), time.monotonic()
	watermark = None if triggered_by == "Rebuild" else control.watermark
	log = frappe.get_doc({"doctype": RUN_LOG, "job": job, "triggered_by": triggered_by, "status": "Running",
	                      "started_at": started, "watermark_from": watermark})
	log.insert(ignore_permissions=True)  # authz-ok: tier-a — the worker records its own run
	control.db_set({"last_status": "Running", "last_run_at": started}, update_modified=False)
	frappe.db.commit()
	try:
		counts = model.refresh(frappe._dict(started=started, watermark=watermark, write=write, checkpoint=frappe.db.commit,
		                                    window_days=window_days(model, control)))
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title=f"Tatva Analytics: {job} failed")
		finish(control, log, clock, "Failed", error=frappe.get_traceback())
		return
	finish(control, log, clock, "Success", watermark=started, **counts)
	for after in frappe.get_all(CONTROL, filters={"enabled": 1, "after_job": job}, pluck="name"):
		queue(after, "After job")


def finish(control, log, clock, status, error=None, watermark=None, changed=0, written=0, deleted=0):
	secs = round(time.monotonic() - clock, 1)
	log.db_set({"status": status, "ended_at": now_datetime(), "watermark_to": watermark, "rows_changed": changed,
	            "rows_written": written, "rows_deleted": deleted, "secs": secs, "error": error})
	state = {"last_status": status, "last_rows": written, "last_secs": secs, "last_error": error}
	if watermark:
		state["watermark"] = watermark
	control.db_set(state, update_modified=False)
	frappe.db.commit()


def write(doctype, rows):
	"""Upsert rows on their unique grain_key, stamped as_of now: a new row gets its own id, an existing one keeps it; return how many."""
	if not rows:
		return 0
	rows = [{**row, "name": row.get("name") or frappe.generate_hash()} for row in rows]
	table, now = frappe.qb.DocType(doctype), now_datetime()
	columns = [*rows[0], "as_of", "creation", "modified"]
	for start in range(0, len(rows), CHUNK):
		query = frappe.qb.into(table).columns(*columns).insert(*[(*row.values(), now, now, now) for row in rows[start:start + CHUNK]])
		for column in columns:
			if column not in ("name", "creation"):
				query = query.on_duplicate_key_update(table[column], Values(table[column]))
		query.run()
	return len(rows)


def sync_controls():
	"""after_migrate: one Control row per model, created off; code owns target, window limit and a declared parent, the operator the rest."""
	for model in models.MODELS:
		owned = {"target": model.TARGET, "window_default": model.WINDOW_DAYS, "window_limit": window_limit(model)}
		if getattr(model, "AFTER", None):
			owned.update(after_job=model.AFTER, frequency=None, cron_format=None)
		if frappe.db.exists(CONTROL, model.JOB):
			frappe.db.set_value(CONTROL, model.JOB, owned, update_modified=False)
		else:
			frappe.get_doc({"doctype": CONTROL, "job": model.JOB, **owned}).insert(ignore_permissions=True)  # authz-ok: tier-a — migrate


def assert_models():
	"""after_migrate: every Control row has a model, the frequencies match the form, and every query returns exactly its table's fields."""
	stray = set(frappe.get_all(CONTROL, pluck="name")) - set(models.by_job())
	if stray:
		frappe.throw(f"Tatva Analytics Control rows with no model in code: {sorted(stray)}")
	offered = set((frappe.get_meta(CONTROL).get_field("frequency").options or "").split("\n")) - {"", "Cron"}
	if offered != set(FREQUENCIES):
		frappe.throw(f"Tatva Analytics Control frequencies {sorted(offered)} differ from engine.FREQUENCIES {sorted(FREQUENCIES)}")
	for model in models.MODELS:
		if model.WINDOW_DAYS and not 1 <= model.WINDOW_DAYS <= window_limit(model):
			frappe.throw(f"{model.JOB}: WINDOW_DAYS {model.WINDOW_DAYS} is outside 1..{window_limit(model)}")
		returned = set(query_columns(model)) - {"name"}
		declared = {df.fieldname for df in frappe.get_meta(model.TARGET).fields if df.fieldname != "as_of"}
		if returned != declared:
			frappe.throw(f"{model.TARGET} drifted from its query. Query only: {sorted(returned - declared)}; "
			             f"DocType only: {sorted(declared - returned)}")


def query_columns(model):
	"""The columns a model's query returns, read from an empty run."""
	never = "1900-01-01"
	probe = {"lo": never, "hi": never, "write_lo": never, "write_hi": never, "agents": ("",), "media": ("",)}
	# A model whose query binds other values names them in PROBE.
	frappe.db.sql(f"SELECT * FROM ({model.select_sql()}) q LIMIT 0",  # sqli-ok: the model's own constant SQL, values bound
	              {**probe, **getattr(model, "PROBE", {})})
	return [column[0] for column in frappe.db.get_description()]


def apply_switch(enabled):
	"""Activator for Analytics::Pipeline::runs: Log Settings clears the run log only while analytics runs."""
	settings = frappe.get_doc("Log Settings")
	row = next((r for r in settings.logs_to_clear if r.ref_doctype == RUN_LOG), None)
	if enabled and not row:
		settings.append("logs_to_clear", {"ref_doctype": RUN_LOG, "days": RUN_LOG_DAYS})
	elif row and not enabled:
		settings.remove(row)
	else:
		return
	settings.save(ignore_permissions=True)  # authz-ok: tier-a — operator activator
