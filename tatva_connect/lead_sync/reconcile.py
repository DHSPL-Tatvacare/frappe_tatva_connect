# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Validate a Facebook Lead Form against Meta: which of Meta's leads reached the CRM, and what happened to the rest.

FOUR ANSWERS, NEVER ONE BUCKET. An id Meta holds that no CRM lead carries is not the same fact as a lead
that never arrived: most of them are the SAME PERSON already in the CRM, matched later by phone, whose
submission was simply never stamped onto them. So every lead is placed by BOTH keys the fold itself uses —
`facebook_lead_id` first, then the identity `leads.dedup_anchor` decides (phone + product line + group) —
and only a lead that answers to neither is reported as MISSING, which is the one state that means the sync
lost something.

Reading the person costs nothing extra at Meta: the check asks its own pager for the fields the crawl asks
for, so the phone arrives on the rows already being read.
"""
import time

import frappe
from frappe import _
from frappe.utils import add_days, now_datetime

from tatva_connect.lead import leads as lead_identity
from tatva_connect.lead_sync.contract import contract_of
from tatva_connect.lead_sync.failure_log import lead_id_of
from tatva_connect.lead_sync.graph import redact_tokens
from tatva_connect.lead_sync.source import TatvaFacebookSyncSource, answers, fold_for
from tatva_connect.whatsapp.phone import to_e164

WINDOW_DAYS = 7
# What the operator may look back over. `None` asks Meta for everything it still holds, which SCAN_CAP still bounds.
WINDOWS = {"7": 7, "14": 14, "30": 30, "all": None}
SCAN_CAP = 5000  # Meta ids one check reads; a busier week is reported as truncated, never paged to the end
PREVIEW_ROWS = 10  # rows the dialog lists; the counts carry the whole window and a re-sync reads the cached ids, not this table
MOBILE_KEY = "lead:mobile_no"  # the catalog key a form question maps to when it asks for the phone
_CHUNK = 500  # ids per IN (...) lookup on the UNIQUE facebook_lead_id index
_BUCKET_TTL = 600  # how long a check's own verdict stays the only thing a re-sync may fold

# Named once so the worker and the form cannot drift; all are `user=`-targeted, as the import's events are.
EVENT_READY, EVENT_FAILED = "fb_check_ready", "fb_check_failed"
EVENT_RESYNC_PROGRESS, EVENT_RESYNC_DONE = "fb_resync_progress", "fb_resync_done"

# The three states a lead that is not linked can be in, in the words the report uses.
IN_CRM, FAILED, MISSING = "in_crm", "failed", "missing"


def check(form, days=WINDOW_DAYS):
	"""Meta's last `days` of leads for `form`, placed by id and then by person. `days=None` asks for everything.

	The counts cover the window; `rows` is a PREVIEW of the newest few. A bad week is hundreds of rows, and a
	dialog is not a report - the numbers answer "is this form healthy", the preview answers "what does a bad one
	look like", and a re-sync works off the cached ids rather than anything this table holds."""
	sources = _sources(form)
	source = frappe.get_doc("Lead Sync Source", sources[0])
	fold = fold_for(source)
	since_unix = (time.time() - days * 86400) if days else None
	leads = fold.list_leads(since_unix, SCAN_CAP + 1)
	meta = {
		lead["id"]: {
			"received": TatvaFacebookSyncSource.site_time(lead.get("created_time")),
			"phone": _phone_of(fold, lead),
		}
		for lead in leads[:SCAN_CAP]
	}
	result = compare(
		meta,
		_in_crm(list(meta)),
		_failed(sources, add_days(now_datetime(), -days) if days else None),
		_people_in_crm(source, meta),
	)
	frappe.cache.set_value(_bucket_key(form), _to_fold(result["rows"]), expires_in_sec=_BUCKET_TTL)
	return {**result, "rows": result["rows"][:PREVIEW_ROWS], "listed": len(result["rows"]),
	        "days": days, "truncated": len(leads) > SCAN_CAP}


def compare(meta, linked, failed, people):
	"""Pure: Meta's leads, the ids the CRM carries, the failure logs and the people it already holds -> counts + rows."""
	rows = []
	for lead_id, fact in sorted(meta.items(), key=lambda item: str(item[1]["received"] or ""), reverse=True):
		if lead_id in linked:
			continue
		log, kind = failed.get(lead_id, (None, None))
		if log:
			state, why = FAILED, kind or _("The sync failed and logged it")
		elif fact["phone"] and fact["phone"] in people:
			state, why = IN_CRM, _("Already a lead here; this submission is not stamped onto it")
		else:
			state, why = MISSING, _("No lead carries this id and none answers to this person")
		rows.append({"lead_id": lead_id, "received": fact["received"], "state": state, "log": log, "why": why})
	counted = {state: sum(1 for row in rows if row["state"] == state) for state in (IN_CRM, FAILED, MISSING)}
	return {"meta": len(meta), "linked": len(meta) - len(rows), "in_crm": counted[IN_CRM],
	        "failed": counted[FAILED], "missing": counted[MISSING], "rows": rows}


def start(form, days):
	"""Queue a check and return at once: a thirty-day form is fifty Graph pages, which is no work for a request to hold."""
	_assert_crawl_credential(form)
	frappe.enqueue(f"{__name__}.run", queue="long", form=form, days=days,
	               job_id=f"fb-check::{frappe.session.user}::{form}", deduplicate=True)


def _assert_crawl_credential(form):
	"""Refuse before queueing when the credential the crawl RUNS on is dead: one call here, or fifty 401s inside a job.

	The token typed on a source is not the one to ask about - `crawl_token` prefers the Page token discovery
	stored, so a fresh paste on the source sits unused behind an expired one and the crawl fails anyway."""
	from tatva_connect.lead_sync.token import app_for, inspect

	source = frappe.get_doc("Lead Sync Source", _sources(form)[0])
	app = app_for(source)
	info, unreachable = inspect(source.crawl_token(), app)
	if info.get("is_valid"):
		return
	frappe.throw(
		_("{0} Run Refresh From Facebook on {1} to store a fresh Page token, then check again.").format(
			unreachable or _("Facebook no longer accepts the token this form crawls with."), app.app_name),
		title=_("Token not accepted"),
	)


def run(form, days):
	"""The queued check: page Meta, then tell the tab that asked how it went."""
	try:
		report = check(form, days)
	except Exception:
		frappe.log_error(title="lead sync: Meta check failed", message=frappe.get_traceback())
		frappe.publish_realtime(EVENT_FAILED, {"form": form}, user=frappe.session.user)
		return
	frappe.publish_realtime(EVENT_READY, {"form": form, **report}, user=frappe.session.user)


def start_resync(form):
	"""Queue the re-sync of everything the last check left unlinked, and say how many that is.

	Queued for the reason the check is: each lead is a Graph call and a commit, which is a worker's work and
	not a request's. The tab follows it on the same progress bar the lead import draws."""
	pending = _pending(form)
	frappe.enqueue(f"{__name__}.run_resync", queue="long", form=form, user=frappe.session.user,
	               job_id=f"fb-resync::{frappe.session.user}::{form}", deduplicate=True)
	return {"queued": True, "total": len(pending)}


def run_resync(form, user):
	"""Fold every unlinked lead the check named, announcing each one, then report what became of them."""
	pending = _pending(form)
	source = frappe.get_doc("Lead Sync Source", _sources(form)[0])
	fold = fold_for(source)
	for done, lead_id in enumerate(pending, start=1):
		# The FOLD guards itself; the re-fetch before it does not, and one Graph timeout used to end the batch.
		try:
			fold.sync_single_lead(fold.fetch_one_lead(lead_id))
		except Exception:
			fold.log_failure({"id": lead_id}, traceback=redact_tokens(frappe.get_traceback(with_context=True)))
		frappe.db.commit()
		frappe.publish_realtime(EVENT_RESYNC_PROGRESS, {"form": form, "processed": done, "total": len(pending)},
		                        user=user)
	frappe.cache.delete_value(_bucket_key(form))
	landed = _in_crm(pending)
	frappe.publish_realtime(EVENT_RESYNC_DONE, {"form": form, "linked": len(landed),
	                                            "failed": len(pending) - len(landed)}, user=user)


def _pending(form):
	"""The ids the last check left unlinked, minus any the crawl has linked since. Refuses on a stale verdict."""
	ids = [i for i in (frappe.cache.get_value(_bucket_key(form)) or []) if i]
	if not ids:
		frappe.throw(_("Run the validation again: there is no recent verdict to re-sync from."),
		             title=_("Nothing to re-sync"))
	linked = _in_crm(ids)
	return [i for i in ids if i not in linked]


def _to_fold(rows):
	"""What a re-sync may fold: everything the check did not find linked, in the order it reported them."""
	return [row["lead_id"] for row in rows]


def _sources(form):
	"""The Lead Sync Sources reading this form, enabled first; the check reads Meta through the first."""
	names = frappe.get_all(  # authz-ok: tier-b — names only; both endpoints are gated on Lead Sync Source permission first
		"Lead Sync Source", filters={"facebook_lead_form": form}, order_by="enabled desc, modified desc", pluck="name")
	if not names:
		frappe.throw(_("No Lead Sync Source reads this form, so there is nothing to compare. Create one first."),
		             title=_("No source"))
	return names


def _phone_of(fold, lead):
	"""This submission's phone in stored form, or None — the form's own mapping decides which answer that is."""
	mapping = fold.get_form_questions_mapping()
	for question, value in answers(lead):
		if mapping.get(question) != MOBILE_KEY or not value:
			continue
		try:
			return to_e164(value)
		except Exception:
			# A number Meta kept but this site cannot store is not a person we can look up; the row says MISSING and why.
			return None
	return None


def _people_in_crm(source, meta):
	"""Which of these submissions' phone numbers already identify a lead — one question, the fold's own grain."""
	contract = contract_of(source)
	return lead_identity.identities_present(
		[fact["phone"] for fact in meta.values()], contract.vertical, contract.crm_group
	)


def _in_crm(ids):
	"""The Meta ids the CRM already holds, looked up on the UNIQUE `facebook_lead_id` index in chunks."""
	held = set()
	for start in range(0, len(ids), _CHUNK):
		held.update(frappe.get_all(  # authz-ok: tier-b — returns only which ids exist, no lead data; gated on Lead Sync Source permission
			"CRM Lead", filters={"facebook_lead_id": ["in", ids[start:start + _CHUNK]]}, pluck="facebook_lead_id"))
	return held


def _failed(sources, since):
	"""{lead_id: (log, type)} from the failure logs of these sources in the window, newest log winning. `since=None` takes them all."""
	out = {}
	filters = {"source": ["in", sources], "type": ["!=", "Synced"]}
	if since:
		filters["creation"] = [">=", since]
	for log in frappe.get_all(  # authz-ok: tier-b — log name and type only; gated on Lead Sync Source permission
			"Failed Lead Sync Log", filters=filters,
			fields=["name", "type", "lead_data"], order_by="creation desc"):
		out.setdefault(lead_id_of(log.lead_data), (log.name, log.type))
	return out


def _bucket_key(form):
	"""This user's own verdict for this form: a re-sync folds what THEY were shown, never another tab's list."""
	return f"fb-check-unlinked::{frappe.session.user}::{form}"
