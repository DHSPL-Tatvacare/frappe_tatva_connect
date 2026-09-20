# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Check a Facebook Lead Form against Meta: which of Meta's recent leads reached the CRM, failed, or went missing."""
import time

import frappe
from frappe import _
from frappe.utils import add_days, now_datetime

from tatva_connect.lead_sync.failure_log import lead_id_of
from tatva_connect.lead_sync.graph import redact_tokens
from tatva_connect.lead_sync.source import TatvaFacebookSyncSource, fold_for

WINDOW_DAYS = 7
SCAN_CAP = 5000  # Meta ids one check reads; a busier week is reported as truncated, never paged to the end
RESYNC_CAP = 20  # leads one click re-fetches and folds inside the request
PREVIEW_ROWS = 10  # rows the dialog lists; the counts carry the whole week and re-sync reads the cached ids, not this table
_CHUNK = 500  # ids per IN (...) lookup on the UNIQUE facebook_lead_id index
_MISSING_TTL = 600  # how long a check's missing list stays the only thing a re-sync may fold


def check(form):
	"""Meta's last WINDOW_DAYS of leads for `form`, bucketed: in the CRM, failed with a log, or missing.

	The counts cover the window; `rows` is a PREVIEW of the newest few. A bad week is hundreds of rows, and a
	dialog is not a report - the numbers answer "is this form healthy", the preview answers "what does a bad one
	look like", and re-sync works off the cached ids rather than anything this table holds."""
	sources = _sources(form)
	since_unix = time.time() - WINDOW_DAYS * 86400
	leads = fold_for(frappe.get_doc("Lead Sync Source", sources[0])).list_lead_ids(since_unix, SCAN_CAP + 1)
	meta = {lead["id"]: TatvaFacebookSyncSource.site_time(lead.get("created_time")) for lead in leads[:SCAN_CAP]}
	result = compare(meta, _in_crm(list(meta)), _failed(sources, add_days(now_datetime(), -WINDOW_DAYS)))
	frappe.cache.set_value(_missing_key(form), [r["lead_id"] for r in result["rows"] if r["state"] == "Missing"],
	                       expires_in_sec=_MISSING_TTL)
	return {**result, "rows": result["rows"][:PREVIEW_ROWS], "listed": len(result["rows"]),
	        "days": WINDOW_DAYS, "truncated": len(leads) > SCAN_CAP, "resync_cap": RESYNC_CAP}


def compare(meta, in_crm, failed):
	"""Pure: {lead_id: received} from Meta, the ids the CRM holds, {lead_id: (log, type)} -> the four counts and the rows to act on."""
	rows = []
	for lead_id, received in sorted(meta.items(), key=lambda item: str(item[1] or ""), reverse=True):
		if lead_id in in_crm:
			continue
		log, kind = failed.get(lead_id, (None, None))
		rows.append({"lead_id": lead_id, "received": received, "state": "Failed" if log else "Missing",
		             "log": log, "why": kind or _("Not in the CRM and no failure was logged")})
	missing = sum(1 for row in rows if row["state"] == "Missing")
	return {"meta": len(meta), "in_crm": len(meta) - len(rows), "failed": len(rows) - missing,
	        "missing": missing, "rows": rows}


def resync(form):
	"""Fold the leads the last check found missing, at most RESYNC_CAP, through the crawl's own fold; one commit per lead."""
	sources = _sources(form)
	source = frappe.get_doc("Lead Sync Source", sources[0])
	frappe.has_permission("Lead Sync Source", "write", doc=source, throw=True)
	ids = [i for i in (frappe.cache.get_value(_missing_key(form)) or []) if i]
	if not ids:
		frappe.throw(_("Run Check against Meta again: there is no recent list of missing leads to re-sync."),
		             title=_("Nothing to re-sync"))
	ids = [i for i in ids if i not in _in_crm(ids)][:RESYNC_CAP]
	fold = fold_for(source)
	for lead_id in ids:
		# The FOLD guards itself; the re-fetch before it does not, and one Graph timeout used to end the batch.
		try:
			fold.sync_single_lead(fold.fetch_one_lead(lead_id))
		except Exception:
			fold.log_failure({"id": lead_id}, traceback=redact_tokens(frappe.get_traceback(with_context=True)))
		frappe.db.commit()
	frappe.cache.delete_value(_missing_key(form))
	landed = _in_crm(ids)
	return {"synced": len(landed), "failed": len(ids) - len(landed)}


def _sources(form):
	"""The Lead Sync Sources reading this form, enabled first; the check reads Meta through the first."""
	names = frappe.get_all(  # authz-ok: tier-b — names only; both endpoints are gated on Lead Sync Source permission first
		"Lead Sync Source", filters={"facebook_lead_form": form}, order_by="enabled desc, modified desc", pluck="name")
	if not names:
		frappe.throw(_("No Lead Sync Source reads this form, so there is nothing to compare. Create one first."),
		             title=_("No source"))
	return names


def _in_crm(ids):
	"""The Meta ids the CRM already holds, looked up on the UNIQUE `facebook_lead_id` index in chunks."""
	held = set()
	for start in range(0, len(ids), _CHUNK):
		held.update(frappe.get_all(  # authz-ok: tier-b — returns only which ids exist, no lead data; gated on Lead Sync Source permission
			"CRM Lead", filters={"facebook_lead_id": ["in", ids[start:start + _CHUNK]]}, pluck="facebook_lead_id"))
	return held


def _failed(sources, since):
	"""{lead_id: (log, type)} from the failure logs of these sources in the window, newest log winning."""
	out = {}
	for log in frappe.get_all(  # authz-ok: tier-b — log name and type only; gated on Lead Sync Source permission
			"Failed Lead Sync Log", filters={"source": ["in", sources], "creation": [">=", since], "type": ["!=", "Synced"]},
			fields=["name", "type", "lead_data"], order_by="creation desc"):
		out.setdefault(lead_id_of(log.lead_data), (log.name, log.type))
	return out


def _missing_key(form):
	return f"tatva_connect:fb_missing:{frappe.session.user}:{form}"
