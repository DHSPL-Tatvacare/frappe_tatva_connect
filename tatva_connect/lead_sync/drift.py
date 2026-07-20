"""Form drift: a Facebook form that was duplicated or recreated gets a NEW id, so a source still pointing
at the old one crawls a dead form — Graph answers 0 leads, nothing errors, and the leads simply stop.

The crawl asks Facebook which forms the Page actually has and says so out loud when the configured one is
not among them. Read only: it lists, it never stores a form and never touches a lead.
"""
import frappe

from tatva_connect.lead_sync.discovery import list_forms
from tatva_connect.lead_sync.graph import redact_tokens
from tatva_connect.lead_sync.token import page_of_form

LOG_TYPE_MISSING = "Unconfigured Form"
LOG_TYPE_QUESTIONS = "Form Questions Changed"
LOG_TYPE_UNSOURCED = "Form Without A Source"


def report_form_drift(source) -> bool:
	"""True when the source's configured form has drifted; logs it once per crawl.

	Never raises: a drift check that breaks the crawl is worse than the drift it reports."""
	try:
		page, token = page_of_form(source.facebook_lead_form)
		if not (page and token):
			return False
		live_forms = list_forms(page, token)
		if not live_forms:
			return False
		live = {f.get("id") for f in live_forms}
		if source.facebook_lead_form not in live:
			_log_missing(source, live)
			return True
		# Independent of the id check: a duplicate leaves the original live, so only an uncrawled form shows it.
		unsourced = _report_unsourced_forms(source, live)
		return _report_question_drift(source, live_forms) or unsourced
	except Exception:
		frappe.log_error(
			title=f"Facebook form drift check failed: {source.name}",
			message=redact_tokens(frappe.get_traceback(with_context=True)),
		)
		return False


def _report_question_drift(source, live_forms: list) -> bool:
	"""True when the form kept its id but its question set changed, which a form-id comparison cannot see.
	An edited form is far commoner than a recreated one, so the questions are what is actually compared."""
	live_form = next((f for f in live_forms if f.get("id") == source.facebook_lead_form), {})
	live_questions = live_form.get("questions") or []
	# An absent `questions` key means Graph was not asked, which is not the same as a form carrying none.
	if not live_questions:
		return False
	live_keys = {q.get("key") for q in live_questions}
	stored_keys = set(
		frappe.get_all(
			"Facebook Lead Form Question",
			filters={"parent": source.facebook_lead_form},
			pluck="key",
		)
	)
	added, removed = live_keys - stored_keys, stored_keys - live_keys
	if not (added or removed):
		return False
	_log_questions(source, added, removed)
	return True


def _report_unsourced_forms(source, live_ids: set) -> bool:
	"""True when the Page carries a live lead form that NO source crawls — what a duplicated form looks like.

	Duplicating is the ordinary way to edit a published form, and it mints a new id while leaving the
	original in place. Nothing else here notices: the configured id is still live, so the id check passes,
	the old form quietly stops receiving submissions, and the new one is crawled by nobody."""
	ids = {i for i in live_ids if i}
	if not ids:
		return False
	sourced = set(frappe.get_all(
		"Lead Sync Source",
		filters={"facebook_lead_form": ("in", sorted(ids))},
		pluck="facebook_lead_form",
	))
	orphans = ids - sourced
	if not orphans:
		return False
	_log(
		source,
		LOG_TYPE_UNSOURCED,
		{"unsourced_forms": sorted(orphans)},
		"Facebook reports lead forms on this Page that no Lead Sync Source crawls. A duplicated form gets "
		"a new id and leaves the original live, so submissions move to the new form while this source keeps "
		"reading the old one. Add a source for it with the same contract, and its mappings carry over.",
	)
	return True


def _log_missing(source, live_ids: set) -> None:
	"""Names the dead id and what Facebook does report, so the source can be repointed without reading a traceback."""
	_log(
		source,
		LOG_TYPE_MISSING,
		{"configured_form": source.facebook_lead_form, "live_forms": sorted(live_ids)},
		f"Form {source.facebook_lead_form} is not among the lead forms Facebook reports for this Page. "
		"A duplicated or recreated form gets a new id, so this source needs repointing at the live form.",
	)


def _log_questions(source, added: set, removed: set) -> None:
	"""Names what changed, so a reworded question is seen as the new unmapped key it is rather than as missing data."""
	_log(
		source,
		LOG_TYPE_QUESTIONS,
		{"form": source.facebook_lead_form, "added": sorted(added), "removed": sorted(removed)},
		f"Form {source.facebook_lead_form} kept its id but its questions changed. "
		"Refresh the form from Facebook, then map any added question to a screening concept.",
	)


def _log(source, log_type: str, payload: dict, explanation: str) -> None:
	"""THE drift log writer, so both drift kinds land in one shape the operator already reads.

	A given drift is reported ONCE while it persists. The crawl runs every five to fifteen minutes and
	the drift it finds is the same drift every pass, so an unconditional insert wrote the same row up to
	288 times a day and buried the log it was meant to be read from. "Once" means: nothing is written
	while the newest row of this type for this source already describes exactly this drift. A drift that
	CHANGES writes again, because the payload differs, and a drift that returns after the operator has
	cleared the log writes again too, which is what makes the log safe to clear."""
	if _already_reported(source, log_type, payload):
		return
	frappe.get_doc({
		"doctype": "Failed Lead Sync Log",
		"type": log_type,
		"source": source.name,
		"lead_data": frappe.as_json(payload),
		"traceback": explanation,
	}).insert(ignore_permissions=True)  # authz-ok: tier-a — scheduler lane, operator-visible log of their own source
	# The crawl rolls back on failure, so the row that explains it is committed or it dies with what it describes.
	frappe.db.commit()


def _already_reported(source, log_type: str, payload: dict) -> bool:
	"""True when the newest row of this type for this source already describes this same drift.
	Compared as parsed JSON rather than as text, so key ordering cannot fake a change."""
	latest = frappe.get_all(
		"Failed Lead Sync Log",
		filters={"source": source.name, "type": log_type},
		fields=["lead_data"],
		order_by="creation desc",
		limit=1,
	)
	if not latest:
		return False
	try:
		return frappe.parse_json(latest[0].lead_data) == payload
	except Exception:
		return False
