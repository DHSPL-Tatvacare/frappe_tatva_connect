"""Jobs this site installs but can never use — stopped, with the reason written down.

Every installed app contributes its `scheduler_events` to OUR scheduler, whether or not this CRM uses
the feature behind them. `bench migrate` creates a `Scheduled Job Type` row per declared method and
starts it. Nothing upstream asks whether the doctype it sweeps has a single row on this site.

The audit behind this list is `docs/investigations/scheduled-jobs-audit.md`. Read it before adding or
removing an entry — in particular, do NOT infer a job's usefulness from an app's `hooks.py`: the source
on disk is not always the version in the container. **The `Scheduled Job Type` table is the truth.**

**This is not the automation toggle plane, and must never overlap it.** A `CRM Tatva Automation` toggle
owns its own scheduled job (registry `Auto.backs` + `Auto.activator`, e.g. `observability.rollup.
apply_rollup`) and the OPERATOR decides whether it runs. This list answers a different question — is
this third-party job usable on this site at all — and the answer is permanently no, so there is no
toggle and nothing to enable. A method in both would mean the operator switches a feature on and the
next migrate silently switches it off; `tests/static/test_scheduler_denylist.py` forbids the overlap.

Stopping a SWEEP is not sealing a FEATURE. `frappe_whatsapp` still registers `doc_events` on `"*"`, so
a `WhatsApp Notification` created in the desk fires on doc events regardless of anything here (and it
resolves the default outgoing account, bypassing our grain routing). That door needs a permission
lockdown, not a scheduler flag.

Why `after_migrate` and not a patch: patches run BEFORE `sync_jobs()` creates the rows (migrate.py:139
vs :162), so a patch no-ops on a fresh site. `after_migrate` runs at :200, once the rows exist. The flag
survives every later migrate because `insert_single_event` only ever rewrites `frequency`/`cron_format`
on an existing row and preserves the rest; the scheduler enqueues from `filters={"stopped": 0}`.
"""
import frappe

# (method, why it can never do anything on this site)
DENYLIST = [
	# frappe_whatsapp: all ten sweep `WhatsApp Notification` — automated WhatsApp goes through the engine's Send WhatsApp verb (automation/sends.py), which never touches that doctype.
	("frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_notification.whatsapp_notification.trigger_notifications", "WhatsApp Notification (Days Before/After) — unused doctype"),
	("frappe_whatsapp.utils.trigger_whatsapp_notifications_all", "WhatsApp Notification (event_frequency=All) — unused doctype"),
	("frappe_whatsapp.utils.trigger_whatsapp_notifications_hourly", "WhatsApp Notification (Hourly) — unused doctype"),
	("frappe_whatsapp.utils.trigger_whatsapp_notifications_hourly_long", "WhatsApp Notification (Hourly Long) — unused doctype"),
	("frappe_whatsapp.utils.trigger_whatsapp_notifications_daily", "WhatsApp Notification (Daily) — unused doctype"),
	("frappe_whatsapp.utils.trigger_whatsapp_notifications_daily_long", "WhatsApp Notification (Daily Long) — unused doctype"),
	("frappe_whatsapp.utils.trigger_whatsapp_notifications_weekly", "WhatsApp Notification (Weekly) — unused doctype"),
	("frappe_whatsapp.utils.trigger_whatsapp_notifications_weekly_long", "WhatsApp Notification (Weekly Long) — unused doctype"),
	("frappe_whatsapp.utils.trigger_whatsapp_notifications_monthly", "WhatsApp Notification (Monthly) — unused doctype"),
	("frappe_whatsapp.utils.trigger_whatsapp_notifications_monthly_long", "WhatsApp Notification (Monthly Long) — unused doctype"),
	# frappe — phone-home and desk features with nothing behind them here.
	("frappe.integrations.doctype.google_calendar.google_calendar.sync", "pulls enabled Google Calendars — zero rows, no integration; ran every 4 minutes"),
	("frappe.utils.telemetry.pulse.client.send_queued_events", "outbound telemetry to Frappe — this is a PHI site"),
	("frappe.utils.change_log.check_for_update", "version phone-home to api.github.com — DevOps owns upgrades"),
	("frappe.desk.doctype.changelog_feed.changelog_feed.fetch_changelog_feed", "fetches Frappe's what's-new feed for the desk"),
	("frappe.automation.doctype.auto_repeat.auto_repeat.make_auto_repeat_entry", "recurring-doc engine — zero Auto Repeat rows; enqueues a long job daily regardless"),
	("frappe.email.doctype.auto_email_report.auto_email_report.send_daily", "scheduled report emails — zero Auto Email Report rows"),
	("frappe.email.doctype.auto_email_report.auto_email_report.send_monthly", "scheduled report emails — zero Auto Email Report rows"),
	("frappe.website.doctype.personal_data_deletion_request.personal_data_deletion_request.process_data_deletion_request", "GDPR portal deletion flow — zero rows; no-ops unless auto_account_deletion >= 1"),
	("frappe.website.doctype.personal_data_deletion_request.personal_data_deletion_request.remove_unverified_record", "GDPR portal deletion flow — zero rows"),
	("frappe.website.doctype.web_page.web_page.check_publish_status", "publishes/unpublishes Web Pages by date — zero Web Page rows"),
	# frappe — Document Follow: a DESK-only email digest of changes to docs a user follows. All six switches
	# live on User > More Information and every one ships 0, so nobody follows anything: measured 0 of 255
	# users with any switch on, and 0 Document Follow rows. The SPA has no follow surface at all.
	("frappe.desk.form.document_follow.send_daily_updates", "Document Follow digest — 0 followers, 0 rows; Desk-only feature the SPA never exposes"),
	("frappe.desk.form.document_follow.send_hourly_updates", "Document Follow digest (hourly) — same, 0 followers"),
	("frappe.desk.form.document_follow.send_weekly_updates", "Document Follow digest (weekly) — same, 0 followers"),
	("frappe.desk.doctype.event.event.send_event_digest", "mails a digest of frappe's own calendar Events — zero Event rows; our work lives on CRM Task, a different doctype"),
	("frappe.core.doctype.prepared_report.prepared_report.expire_stalled_report", "expires background-run Desk reports — zero Prepared Report rows"),
	# helpdesk — the corpus is three NLTK language files (POS tagger, tokenizer, Brown corpus) for ticket
	# search. It is a download, on the `All` cadence, reaching out to nltk's servers from a PHI site.
	("helpdesk.search.download_corpus", "downloads NLTK language files on every `All` tick — outbound fetch from a PHI site; ticket search runs on what is already on disk"),
	# insights — the scheduled-import timer, the same family as the CSV/Excel import that is switched off here.
	("insights.insights.doctype.insights_table_import_job.insights_table_import_job.run_scheduled_imports", "timed Insights data imports — zero Import Job rows and file import is disabled on this site"),
	# lms — features of the LMS we do not run (the rest of its jobs stay on).
	("lms.job.doctype.job_opportunity.job_opportunity.update_job_openings", "LMS job board — not a feature we run"),
	("lms.lms.doctype.lms_payment.lms_payment.send_payment_reminder", "LMS paid-course reminders — no paid courses"),
	("lms.lms.doctype.lms_certificate_request.lms_certificate_request.schedule_evals", "books Google Meet slots for certificate evaluations — zero Certificate Request rows; internal training is not certified"),
	("lms.lms.doctype.lms_certificate_request.lms_certificate_request.mark_eval_as_completed", "closes those evaluation slots — same, zero rows"),
	("lms.lms.doctype.lms_live_class.lms_live_class.update_attendance", "pulls attendee lists back from ZOOM for a finished live class — zero Live Class rows, no Zoom account"),
	("lms.lms.doctype.lms_live_class.lms_live_class.send_live_class_reminder", "mails students before a live class — same, zero rows"),
	# insights — the alert mailer only; its three sibling jobs stay ON. Reverses the audit's "left ON" call, which was wrong.
	("insights.insights.doctype.insights_alert.insights_alert.send_alerts", "Insights Alert mailer — validate() checks read at SAVE, then this runs the query from the scheduler as Administrator and mails the rows to free-text recipients; zero rows, not a CRM feature"),
]

DENYLISTED_METHODS = frozenset(method for method, _reason in DENYLIST)


def apply():
	"""Stop every denylisted job. Idempotent, and silent about a method whose row does not exist (an app
	we no longer install, or a job an upstream release renamed) — `tests/static/test_scheduler_denylist.py`
	is what catches a rotted entry, not a throw here that would fail the whole migrate."""
	for method, _reason in DENYLIST:
		for name in frappe.get_all("Scheduled Job Type", filters={"method": method, "stopped": 0}, pluck="name"):
			frappe.db.set_value("Scheduled Job Type", name, "stopped", 1)
