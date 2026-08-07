# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""One read, one payload: the state of the union. Not published, not documented, not for a partner.

Frappe already collects the platform half and does it better than we would — `System Health Report` is a
virtual doctype whose collectors run on read, so `frappe.get_doc(...)` IS the whole call. It knows the
scheduler down to whether its PROCESS is alive, the workers and their queues, the email queue, the error
log, the database, the cache, the file sizes and the sessions. None of that is re-collected here; it is
passed through under `platform`.

What frappe cannot know is that this app exists. Its half is added under `tatva`, collected THE SAME WAY:
every section is a method wearing frappe's own `@health_check`, so a section that raises is suppressed
exactly as a frappe section is, comes back None, and names itself in `sections_failed`. There is no second
error-handling mechanism, no second decorator and no try/except of our own.

`verdict` and `problems` are the only fields worth alerting on. Everything under them is for reading.
"""

import frappe
from frappe.desk.doctype.system_health_report.system_health_report import health_check
from frappe.query_builder.functions import Count
from frappe.utils import add_to_date, cint, now_datetime

def _mb(value):
	"""Megabytes, two places. A file size to fifteen decimals is noise, not precision."""
	return round(float(value or 0), 2)


def _pct(value):
	"""Every percentage in this payload is a string ending in `%`, so it reads without a legend."""
	return f"{round(float(value or 0), 2)}%"


def _num(value):
	"""Back to a number, for the rules below — a formatted payload should not force a second format."""
	return float(str(value or 0).rstrip("%") or 0)


def _job_name(job_id):
	"""A `Scheduled Job Type` name is a hash. Its `method` is what a human recognises."""
	return frappe.db.get_value("Scheduled Job Type", job_id, "method") or job_id


def _job(job_id):
	"""An id alone is unreadable and a name alone cannot be looked up, so a job is reported as both."""
	if not job_id:
		return None
	row = frappe.db.get_value("Scheduled Job Type", job_id, ["method", "frequency", "last_execution"], as_dict=True)
	return {
		"id": job_id,
		"job": (row or {}).get("method"),
		"frequency": (row or {}).get("frequency"),
		"last_execution": str((row or {}).get("last_execution") or "never"),
	}


def _latest(doctype, filters):
	"""When this doctype last saw a row. Ordered on `creation`, which is indexed on every frappe table."""
	rows = frappe.get_all(doctype, filters=filters, fields=["creation"], order_by="creation desc", limit=1)
	return str(rows[0].creation) if rows else None


def _counts_by(doctype, column, days=None):
	"""`{value: count}` in one grouped query. `get_all` refuses a string aggregate on this frappe, and a
	count per distinct value would be N round trips, so the query builder is the door."""
	table = frappe.qb.DocType(doctype)
	query = frappe.qb.from_(table).select(table[column], Count("*").as_("n")).groupby(table[column])
	if days:
		query = query.where(table.creation > add_to_date(now_datetime(), days=-days))
	return {r[column] or "—": r["n"] for r in query.run(as_dict=True)}


def _counts_by_since(doctype, column, days):
	return _counts_by(doctype, column, days=days)


class ControlTower:
	"""Sections are methods; each one returns its own dict and is suppressed independently."""

	def __init__(self):
		self.sections_failed = []

	def _run(self, name, method):
		value = method()
		if value is None:
			self.sections_failed.append(name)
		return value

	@health_check("Platform (frappe System Health Report)")
	def platform(self):
		"""Frappe's own collectors, called individually — every one EXCEPT `fetch_background_jobs`.

		That one is skipped because it ENQUEUES a `frappe.ping` job as its liveness probe, and this
		surface writes nothing at all. The worker and queue facts it would have given are read straight
		off the same sources below, which is a read.
		"""
		report = frappe.new_doc("System Health Report")
		report.fetch_scheduler()
		report.fetch_email_stats()
		report.fetch_errors()
		report.fetch_database_details()
		report.fetch_cache_details()
		report.fetch_storage_details()
		report.fetch_user_stats()
		return {
			"scheduler": {
				"status": report.get("scheduler_status"),
				"failing_jobs": [
					{"job": _job_name(r.get("scheduled_job_type")), "failure_rate": _pct(r.get("failure_rate"))}
					for r in report.get("failing_scheduled_jobs") or []
				],
				"oldest_unscheduled_job": _job(report.get("oldest_unscheduled_job")),
			},
			"workers": self._workers(),
			"email": {
				"sent_30d": report.get("total_outgoing_emails"),
				"pending": report.get("pending_emails"),
				"failed": report.get("failed_emails"),
				"unhandled_incoming": report.get("unhandled_emails"),
			},
			"errors": {
				"total_30d": report.get("total_errors"),
				"top": [{"title": r.get("title"), "count": r.get("occurrences")} for r in report.get("top_errors") or []],
			},
			"database": {
				"type": report.get("database"),
				"version": report.get("database_version"),
				"size_mb": _mb(report.get("db_storage_usage")),
				"buffer_pool_mb": _mb(cint(report.get("bufferpool_size")) / (1024 * 1024)),
				"binary_logging": report.get("binary_logging"),
				"largest_tables": [
					{"table": r.get("table"), "size_mb": _mb(r.get("size"))} for r in report.get("top_db_tables") or []
				],
			},
			"cache": self._cache(report),
			"disk": self._disk(),
			"storage": {
				"private_files_mb": _mb(report.get("private_files_size")),
				"public_files_mb": _mb(report.get("public_files_size")),
				"backups_mb": _mb(report.get("backups_size")),
				"onsite_backups": report.get("onsite_backups"),
			},
			"users": {
				"enabled": report.get("total_users"),
				"new_30d": report.get("new_users"),
				"failed_logins_30d": report.get("failed_logins"),
				"active_sessions": report.get("active_sessions"),
			},
			"realtime": {
				"socketio_ping": report.get("socketio_ping_check"),
				"transport": report.get("socketio_transport_mode"),
			},
		}

	def _workers(self):
		"""Workers and queue depths, read-only. `RQ Worker` is itself a virtual doctype over redis, and
		it carries the utilisation and failure counts frappe's own collector reports."""
		from frappe.utils.background_jobs import get_queue, get_queue_list

		workers = frappe.get_all("RQ Worker")
		return {
			"total": len(workers),
			"detail": [
				{
					"queues": w.queue_type,
					"status": w.status,
					"utilization": _pct(w.utilization_percent),
					"succeeded": w.successful_job_count,
					"failed": w.failed_job_count,
					"last_heartbeat": str(w.last_heartbeat),
				}
				for w in workers
			],
			"queues": [{"queue": q, "pending_jobs": get_queue(q).count} for q in get_queue_list()],
		}

	def _cache(self, report):
		"""Redis's own limits alongside its usage — `noeviction` with a cap means a full Redis starts
		REFUSING writes rather than shedding keys, which is a different incident to a slow cache."""
		config = frappe.cache.execute_command("CONFIG", "GET", "maxmemory") or []
		policy = frappe.cache.execute_command("CONFIG", "GET", "maxmemory-policy") or []
		as_text = lambda raw, i: (raw[i].decode() if isinstance(raw[i], bytes) else str(raw[i])) if len(raw) > i else None
		return {
			"keys": report.get("cache_keys"),
			"memory": report.get("cache_memory_usage"),
			# Redis reports no cap as 0. Saying `0.0 MB` reads as "no memory", which is the opposite.
			"maxmemory_mb": _mb(cint(as_text(config, 1)) / (1024 * 1024)) or "uncapped",
			"eviction_policy": as_text(policy, 1),
		}

	def _disk(self):
		"""Frappe reports what it has stored and never what is left. Attachments and `tabDeleted Document`
		grow without bound, and a full disk takes the site down with no warning anywhere."""
		import shutil

		usage = shutil.disk_usage(frappe.get_site_path())
		return {
			"total_gb": round(usage.total / 1e9, 2),
			"free_gb": round(usage.free / 1e9, 2),
			"used": _pct(100 * usage.used / usage.total),
		}

	@health_check("Configuration")
	def config(self):
		"""What the site is CONFIGURED to allow, as opposed to what it is currently doing.

		Email is asked of the accounts rather than of a setting: frappe has no "email is on" flag, an
		account carries `enable_outgoing`, and a site with no enabled account simply cannot send.
		"""
		accounts = frappe.get_all(
			"Email Account",
			fields=["name", "email_id", "enable_outgoing", "enable_incoming", "default_outgoing", "default_incoming"],
		)
		settings = frappe.get_single("System Settings")
		conf = frappe.get_conf()
		return {
			"email": {
				"outgoing_enabled": any(a.enable_outgoing for a in accounts),
				"incoming_enabled": any(a.enable_incoming for a in accounts),
				"default_outgoing": next((a.email_id for a in accounts if a.default_outgoing), None),
				"default_incoming": next((a.email_id for a in accounts if a.default_incoming), None),
				"accounts": [
					{
						"name": a.name,
						"email": a.email_id,
						"outgoing": bool(a.enable_outgoing),
						"incoming": bool(a.enable_incoming),
					}
					for a in accounts
				],
			},
			"security": {
				"two_factor_auth": bool(settings.enable_two_factor_auth),
				"two_factor_method": settings.two_factor_method,
				"password_policy": bool(settings.enable_password_policy),
				"minimum_password_score": cint(settings.minimum_password_score),
				"session_expiry": settings.session_expiry,
				"deny_multiple_sessions": bool(settings.deny_multiple_sessions),
				"allow_error_traceback": bool(settings.allow_error_traceback),
				"apply_strict_user_permissions": bool(settings.apply_strict_user_permissions),
				"disable_document_sharing": bool(settings.disable_document_sharing),
				"allowed_login_attempts": settings.allow_consecutive_login_attempts,
			},
			"uploads": {
				"guests_may_upload": bool(settings.allow_guests_to_upload_files),
				"public_uploads_system_manager_only": bool(settings.only_allow_system_managers_to_upload_public_files),
				"allowed_file_extensions": [e for e in (settings.allowed_file_extensions or "").splitlines() if e],
				"max_file_size_mb": _mb(cint(settings.max_file_size) / (1024 * 1024)),
				"strip_exif": bool(settings.strip_exif_metadata_from_uploaded_images),
			},
			"backups": {"limit": settings.backup_limit, "encrypted": bool(settings.encrypt_backup)},
			"site": {
				"maintenance_mode": bool(conf.get("maintenance_mode")),
				"pause_scheduler": bool(conf.get("pause_scheduler")),
				"disable_scheduler": bool(conf.get("disable_scheduler")),
				"developer_mode": bool(conf.get("developer_mode")),
				"server_scripts": bool(conf.get("server_script_enabled")),
				"read_from_replica": bool(conf.get("read_from_replica")),
				"scheduler_enabled_in_system_settings": bool(frappe.get_system_settings("enable_scheduler")),
			},
		}

	@health_check("Business heartbeat")
	def heartbeat(self):
		"""Whether work is actually FLOWING. Every other section says the platform is up; this is the only
		one that notices a supplier going quiet, which fails with no error, no queue and no red anywhere."""
		day = add_to_date(now_datetime(), hours=-24)
		leads = frappe.qb.DocType("CRM Lead")
		by_source = (
			frappe.qb.from_(leads)
			.select(leads.source, Count("*").as_("n"))
			.where(leads.creation > day)
			.groupby(leads.source)
			.run(as_dict=True)
		)
		return {
			"leads_24h": frappe.db.count("CRM Lead", {"creation": [">", day]}),
			"leads_7d": frappe.db.count("CRM Lead", {"creation": [">", add_to_date(now_datetime(), days=-7)]}),
			"leads_24h_by_source": {r["source"] or "—": r["n"] for r in by_source},
			"tasks_created_24h": frappe.db.count("CRM Task", {"creation": [">", day]}),
			"tasks_overdue": frappe.db.count("CRM Task", {"status": ["!=", "Done"], "due_date": ["<", now_datetime()]}),
		}

	@health_check("Channels")
	def channels(self):
		"""Configured is not working. Each channel reports its last real traffic, because credentials
		expire and a number can be de-provisioned without anything here changing."""
		return {
			"whatsapp": {
				"accounts": frappe.db.count("WhatsApp Account"),
				"armed": bool(frappe.db.get_value("CRM Tatva Automation", "WhatsApp::Channel::messaging", "enabled")),
				"last_outbound": _latest("WhatsApp Message", {"type": "Outgoing"}),
				"last_inbound": _latest("WhatsApp Message", {"type": "Incoming"}),
			},
			"voice": {
				"accounts": frappe.db.count("CRM AI Voice Account", {"enabled": 1}),
				"armed": bool(frappe.db.get_value("CRM Tatva Automation", "AI Voice::Channel::calls", "enabled")),
			},
			"telephony": {
				"accounts": frappe.db.count("CRM Telephony Account", {"enabled": 1}),
				"dids": frappe.db.count("CRM Telephony Routing DID"),
				"last_call": _latest("CRM Call Log", {}),
			},
			"email": {"last_sent": _latest("Email Queue", {"status": "Sent"})},
		}

	@health_check("Catalog")
	def catalog(self):
		"""Counts only. Whether a catalog row RESOLVES is the brain's question — a field lands in one of
		four shapes and only `CRM Lead Section` knows which — so it is not re-answered here. A naive
		`get_meta("CRM Lead")` check reports 239 of 325 fields broken and every one of them is fine."""
		return {
			"api_fields": frappe.db.count("CRM Lead API Field"),
			"lead_sections": frappe.db.count("CRM Lead Section"),
			"multi_row_sections": frappe.db.count("CRM Lead Section", {"is_multi_row": 1}),
			"picklist_values": frappe.db.count("CRM Picklist Value"),
			"grains_declared": frappe.db.count("CRM Grain"),
			"task_types": frappe.db.count("CRM Task Type"),
		}

	@health_check("Access census")
	def access(self):
		"""Role creep is the thing an auditor asks about next, and nothing else on this site tracks it."""
		return {
			"system_managers": frappe.db.count("Has Role", {"role": "System Manager"}),
			"enabled_users": frappe.db.count("User", {"enabled": 1}),
			"disabled_users": frappe.db.count("User", {"enabled": 0}),
			"users_with_api_keys": frappe.db.count("User", {"api_key": ["is", "set"], "enabled": 1}),
		}

	@health_check("Tatva switches")
	def switches(self):
		"""Every operator switch, grouped by its own `area` column. The app ships dormant, so which of
		these is ON is the single most load-bearing line on this page."""
		rows = frappe.get_all(
			"CRM Tatva Automation", fields=["name", "area", "enabled", "scheduled_job"], order_by="area asc, name asc"
		)
		by_area = {}
		for r in rows:
			bucket = by_area.setdefault(r.area or "—", {"on": [], "off": []})
			bucket["on" if r.enabled else "off"].append(r.name)
		return {
			"total": len(rows),
			"on": sum(1 for r in rows if r.enabled),
			"scheduled_and_armed": [r.name for r in rows if r.enabled and r.scheduled_job],
			"by_area": by_area,
		}

	@health_check("Tatva workflow engine")
	def workflow(self):
		return {
			"engine_armed": bool(frappe.db.get_value("CRM Tatva Automation", "Workflow::Engine::run", "enabled")),
			"workflows_by_lifecycle": _counts_by("CRM Workflow", "lifecycle_state"),
			"journeys_by_status": _counts_by("CRM Workflow Journey", "status"),
			"signals_by_status": _counts_by("CRM Workflow Signal", "status"),
		}

	@health_check("Tatva storage")
	def storage(self):
		allowlist = frappe.db.get_single_value("CRM Azure Storage Settings", "public_attachment_doctypes") or ""
		return {
			"offload_armed": bool(frappe.db.get_value("CRM Tatva Automation", "Storage::Azure::offload", "enabled")),
			"privacy_armed": bool(frappe.db.get_value("CRM Tatva Automation", "Storage::File::privacy", "enabled")),
			"public_attachment_doctypes": [l.strip() for l in allowlist.replace(",", "\n").splitlines() if l.strip()],
			"files_not_offloaded": frappe.db.count("File", {"custom_uploaded_to_azure": 0, "is_folder": 0}),
			"public_files": frappe.db.count("File", {"is_private": 0, "is_folder": 0}),
			"screening_armed": bool(frappe.db.get_value("CRM Tatva Automation", "Storage::File::screening", "enabled")),
			"scan_verdicts_30d": _counts_by_since("CRM File Scan Log", "verdict", days=30),
		}

	@health_check("Tatva partner API")
	def partner_api(self):
		settings = frappe.db.get_singles_dict("CRM API Metric Settings") or {}
		since = frappe.utils.add_to_date(now_datetime(), hours=-1)
		recent = frappe.db.count("CRM API Request Log", {"creation": [">", since]})
		failed = frappe.db.count("CRM API Request Log", {"creation": [">", since], "status_code": [">=", 400]})
		return {
			"calls_1h": recent,
			"failed_1h": failed,
			"rollup_lag_seconds": cint(settings.get("lag_seconds")),
			"rolled_up_until": str(settings.get("last_rolled_until") or ""),
			"rollup_last_run": str(settings.get("last_run_at") or ""),
		}

	@health_check("Build")
	def build(self):
		last = frappe.get_all("Patch Log", fields=["patch", "creation"], order_by="creation desc", limit=1)
		return {
			"apps": {app: frappe.get_attr(f"{app}.__version__") for app in frappe.get_installed_apps()},
			"last_patch": (last[0].patch or "").split("#")[0].strip() if last else None,
			"last_patch_at": str(last[0].creation) if last else None,
		}


def _problems(payload):
	"""The alerting contract: one flat, human list. Nothing here is a judgement the sections did not make."""
	out = []
	platform, tatva = payload.get("platform") or {}, payload.get("tatva") or {}

	for name in payload["meta"]["sections_failed"]:
		out.append(f"section did not collect: {name}")
	if payload["meta"].get("writes"):
		out.append(f"control tower wrote {payload['meta']['writes']} row(s) — it must not write at all")

	scheduler = (platform.get("scheduler") or {}).get("status")
	if scheduler and scheduler != "Active":
		out.append(f"scheduler: {scheduler}")
	for job in (platform.get("scheduler") or {}).get("failing_jobs") or []:
		out.append(f"scheduled job failing: {job}")

	email = platform.get("email") or {}
	if email.get("failed"):
		out.append(f"email: {email['failed']} failed in the last 30 days")
	for queue in (platform.get("workers") or {}).get("queues") or []:
		if cint(queue.get("pending_jobs")) > 1000:
			out.append(f"queue {queue['queue']}: {queue['pending_jobs']} jobs pending")
	if (platform.get("storage") or {}).get("onsite_backups") == 0:
		out.append("backups: no onsite backup present")

	config = payload.get("config") or {}
	for key in ("maintenance_mode", "pause_scheduler", "disable_scheduler", "developer_mode"):
		if (config.get("site") or {}).get(key):
			out.append(f"site config: {key} is ON")

	mail = config.get("email") or {}
	if not mail.get("outgoing_enabled"):
		out.append("email: no outgoing account is enabled — nothing can send")
	elif not mail.get("default_outgoing"):
		out.append("email: an account can send but none is the default")

	security = config.get("security") or {}
	if security.get("allow_error_traceback"):
		out.append("security: stack traces are returned to the browser")
	if not security.get("two_factor_auth") and not security.get("password_policy"):
		out.append("security: neither two-factor auth nor a password policy is on")
	if (config.get("uploads") or {}).get("guests_may_upload"):
		out.append("uploads: guests may upload files")
	if not (config.get("backups") or {}).get("encrypted"):
		out.append("backups: backups are not encrypted")

	disk = platform.get("disk") or {}
	if _num(disk.get("used")) >= 85:
		out.append(f"disk: {disk['used']} used, {disk['free_gb']} GB free")

	cache = platform.get("cache") or {}
	if cache.get("eviction_policy") == "noeviction" and cache.get("maxmemory_mb"):
		out.append("cache: redis is capped with noeviction — a full cache refuses writes")

	beat = tatva.get("heartbeat") or {}
	if beat and not beat.get("leads_24h"):
		out.append("heartbeat: no leads created in the last 24 hours")

	for name, channel in (tatva.get("channels") or {}).items():
		# Every `last_*` the channel reports, whatever it calls them — a channel that reports no traffic
		# timestamp at all is simply not asked the question.
		seen = [v for k, v in (channel or {}).items() if k.startswith("last_")]
		if channel.get("armed") and seen and not any(seen):
			out.append(f"channels: {name} is armed but has no traffic on record")

	scans = (tatva.get("storage") or {}).get("scan_verdicts_30d") or {}
	if scans.get("Infected"):
		out.append(f"storage: {scans['Infected']} infected upload(s) in 30 days")

	api = tatva.get("partner_api") or {}
	if api.get("failed_1h"):
		out.append(f"partner API: {api['failed_1h']} of {api['calls_1h']} calls failed in the last hour")
	if cint(api.get("rollup_lag_seconds")) > 21600:
		out.append(f"partner API rollup is {cint(api['rollup_lag_seconds']) // 3600}h behind")

	storage = tatva.get("storage") or {}
	if storage.get("offload_armed") and cint(storage.get("files_not_offloaded")) > 100:
		out.append(f"storage: {storage['files_not_offloaded']} files still local with offload armed")

	return out


def collect():
	"""Gather everything. Shared by the Desk page and the endpoint so the two can never disagree.

	READ ONLY, and it is checked rather than asserted in prose: frappe counts every write it makes on a
	connection (`Database.transaction_writes`), so the counter is read before and after and a collector
	that wrote anything is a defect that says so here instead of being discovered later.
	"""
	started = now_datetime()
	writes_before = frappe.db.transaction_writes
	tower = ControlTower()
	payload = {
		"platform": tower._run("platform", tower.platform),
		"config": tower._run("config", tower.config),
		"tatva": {
			name: tower._run(name, getattr(tower, name))
			for name in ("heartbeat", "channels", "switches", "workflow", "storage", "partner_api", "catalog", "access", "build")
		},
		"meta": {
			"site": frappe.local.site,
			"collected_at": str(started),
			"sections_failed": tower.sections_failed,
		},
	}
	payload["meta"]["elapsed_ms"] = cint((now_datetime() - started).total_seconds() * 1000)
	payload["meta"]["writes"] = frappe.db.transaction_writes - writes_before
	payload["problems"] = _problems(payload)
	payload["verdict"] = "ok" if not payload["problems"] else "degraded"
	return payload


@frappe.whitelist()
def state_of_the_union():
	"""The same payload over HTTP, for a monitor or a terminal. Deliberately absent from the published
	API surface: it is not a partner endpoint and it is not documented anywhere a partner reads."""
	frappe.only_for("System Manager")
	return collect()
