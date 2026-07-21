"""Page + lead-form discovery: one unreadable Page never kills the readable ones.

Discovery REFRESHES. It is re-runnable from the source's button and from the nightly job, and every write
below is an upsert, because a Page holds the token minted from whichever user token was current and a form
holds questions marketing edits without warning."""
import frappe

from tatva_connect.lead_sync.form import DISCOVERY_FLAG
from tatva_connect.lead_sync.graph import api_url, graph_get, redact_tokens

SWITCH_FORM_REFRESH = "Lead::Facebook::form-refresh"


def fetch_and_store_pages(access_token: str) -> list[dict]:
	"""Replaces the upstream discovery; called from TatvaLeadSyncSource.before_insert."""
	if not access_token:
		frappe.throw(frappe._("Access token is required"))

	account_details = graph_get("token check (/me)", api_url("me"), {}, access_token)
	if not account_details.get("id"):
		frappe.throw(frappe._("Invalid access token provided for Facebook."))

	pages = graph_get("page listing (/me/accounts)", api_url("/me/accounts"), {}, access_token).get("data", [])

	if not pages:
		# Upstream treats an empty list as success: green toast, blank dropdown, no reason given.
		frappe.throw(
			frappe._(
				"This token can see no Facebook Pages. Grant business_management, and in the consent "
				"dialog select the Business that OWNS the Page — selecting the Page alone is not enough."
			),
			title=frappe._("No Facebook Pages"),
		)

	failures = []
	for page in pages:
		page_id = page["id"]
		_upsert_page(page, account_details)
		try:
			page["forms"] = _fetch_and_store_forms(page_id, page["access_token"])
		except Exception as exc:
			# A Page without MANAGE_LEADS must not kill the Pages that have it.
			page["forms"] = []
			failures.append(f"{page.get('name') or page_id}: {exc}")
			frappe.log_error(
				title=f"Facebook lead forms unavailable: {page.get('name') or page_id}",
				message=redact_tokens(frappe.get_traceback(with_context=True)),
			)

	if failures and not any(page.get("forms") for page in pages):
		frappe.throw(
			frappe._("No Facebook Page returned lead forms.<br><br>{0}").format("<br>".join(failures)),
			title=frappe._("Facebook API Error"),
		)
	if failures:
		frappe.msgprint(
			frappe._("These Pages returned no lead forms and were skipped:<br><br>{0}").format(
				"<br>".join(failures)
			),
			title=frappe._("Partial Facebook sync"),
			indicator="orange",
		)

	return pages


def _upsert_page(page: dict, account_details: dict) -> None:
	"""Refresh, never skip: a Page that already exists still holds the token minted from the PREVIOUS user
	token, so a re-pasted credential would never reach the crawl."""
	values = {
		"page_name": page["name"],
		"category": page["category"],
		"access_token": page["access_token"],
		"account_id": account_details["id"],
	}
	if frappe.db.exists("Facebook Page", page["id"]):
		doc = frappe.get_doc("Facebook Page", page["id"])
		doc.update(values)
		doc.save(ignore_permissions=True)  # authz-ok: tier-c — operator-driven discovery of their own Pages
		return
	frappe.get_doc({"doctype": "Facebook Page", "id": page["id"], **values}).insert(ignore_permissions=True)  # authz-ok: tier-c — operator-driven discovery of their own Pages


def list_forms(page_id: str, page_access_token: str) -> list[dict]:
	"""The live lead forms Facebook reports for a Page — READ ONLY, stores nothing. One lister, two callers: discovery (which then stores) and the crawl's drift check.
	`questions` already carries each question's `options`; the choice list is Facebook's own and is never retyped."""
	return graph_get(
		f"lead form listing for page {page_id}",
		api_url(f"/{page_id}/leadgen_forms"),
		{"fields": "id,name,questions{id,key,label,type,options}", "limit": 15000},
		page_access_token,
	).get("data", [])


def _fetch_and_store_forms(page_id: str, page_access_token: str) -> list[dict]:
	forms = list_forms(page_id, page_access_token)
	for form in forms:
		upsert_lead_form(form, page_id)
	return forms


def upsert_lead_form(form: dict, page_id: str) -> None:
	"""Store a form and its questions, refreshing an existing one. THE form writer: it lives here rather
	than in the fork because it decides what a question row holds, and that is a rule this app owns.

	Skipping an existing form is what hid every edited form: marketing rewords a question far more often
	than it publishes a new one, and a reworded question is a new key that nothing would ever see.

	An ABSENT (or null) `questions` key means Graph was not asked, which is not the same as a form
	carrying none: the stored questions and every operator mapping on them are left exactly as they are.
	The read path holds this same rule in `drift._report_question_drift`, and the writer holds it here.
	An explicitly EMPTY list is Facebook's own answer that the form has no questions, and does clear them."""
	questions = _question_rows(form)
	if frappe.db.exists("Facebook Lead Form", form["id"]):
		doc = frappe.get_doc("Facebook Lead Form", form["id"])
		doc.form_name = form["name"]
		if questions is not None:
			_carry_mappings(doc.questions, questions)
			doc.set("questions", questions)
		doc.flags[DISCOVERY_FLAG] = True
		doc.save(ignore_permissions=True)  # authz-ok: tier-c — operator-driven discovery of their own forms
		return
	doc = frappe.get_doc(
		{"doctype": "Facebook Lead Form", "form_name": form["name"], "id": form["id"],
		 "page": page_id, "questions": questions or []}
	)
	_carry_mappings_from_page(doc, page_id)
	doc.flags[DISCOVERY_FLAG] = True
	doc.insert(ignore_permissions=True)  # authz-ok: tier-c — operator-driven discovery of their own forms


def _question_rows(form: dict):
	"""The stored shape of a form's questions, or None when Graph did not report on them at all."""
	raw = form.get("questions")
	if raw is None:
		return None
	return [
		{
			"id": q.get("id"),
			"key": q.get("key"),
			"label": q.get("label"),
			"type": q.get("type"),
			"options": frappe.as_json(q.get("options") or []),
		}
		for q in raw
	]


def refresh_all_sources() -> None:
	"""Nightly pass: every enabled Facebook source re-runs discovery, so a form published or reworded
	today is visible tomorrow without anyone pressing a button.

	Gated on its own operator switch like every other automation in this app: off, which is how it ships,
	the nightly pass does nothing and the forms stay as the last refresh left them.

	One unreadable source never stops the rest, and a source is skipped rather than repeated when its
	token has already been refreshed on this pass by a source that shares it."""
	from tatva_connect import automation

	if not automation.is_enabled(SWITCH_FORM_REFRESH):
		return
	done_tokens = set()
	for name in frappe.get_all(
		"Lead Sync Source", filters={"type": "Facebook", "enabled": 1}, pluck="name"
	):
		source = frappe.get_doc("Lead Sync Source", name)
		token = source.get_password("access_token", raise_exception=False)
		if not token or token in done_tokens:
			continue
		done_tokens.add(token)
		try:
			fetch_and_store_pages(token)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(
				title=f"Facebook nightly refresh failed: {name}",
				message=redact_tokens(frappe.get_traceback(with_context=True)),
			)
			frappe.db.commit()


def _carry_mappings_from_page(doc, page_id: str) -> None:
	"""A form we have never seen inherits the mappings its siblings on the same Page already carry.

	Duplicating is how a published form gets edited, and the copy arrives with a new id and EVERY question
	unmapped — including the one holding the phone number, without which every lead from that form fails
	to upsert and is logged rather than stored. `_carry_mappings` cannot help: it carries within one form
	document, and a duplicate is a different one.

	Matched on `key`, which Facebook derives from the question text, so the same question carries the same
	key into the copy. A key mapped DIFFERENTLY on two siblings identifies nothing and carries nothing —
	the same rule `_carry_mappings` holds, for the same reason."""
	if not doc.questions:
		return
	siblings = frappe.get_all("Facebook Lead Form", filters={"page": page_id}, pluck="name")
	if not siblings:
		return
	seen, ambiguous = {}, set()
	for row in frappe.get_all(
		"Facebook Lead Form Question",
		filters={"parent": ("in", siblings), "mapped_to_crm_field": ("is", "set")},
		fields=["key", "mapped_to_crm_field"],
	):
		if row.key in seen and seen[row.key] != row.mapped_to_crm_field:
			ambiguous.add(row.key)
			continue
		seen[row.key] = row.mapped_to_crm_field
	for q in doc.questions:
		if q.key in ambiguous:
			continue
		if seen.get(q.key):
			q.mapped_to_crm_field = seen[q.key]


def _carry_mappings(existing_rows, questions: list[dict]) -> None:
	"""A refresh replaces the question rows, so the operator's mapping is carried across onto the question
	it was made against. Without this every refresh would silently unmap every form that had been mapped.

	Facebook's question `id` is the identifier that survives a rewording, so it is matched on first and
	`key` is the fallback for a row stored before an id was kept. A key that appears on more than one
	stored row with DIFFERENT mappings identifies nothing, so it carries NOTHING rather than applying one
	operator decision to a question it was never made for: the form shows both questions unmapped, which
	is the state the operator can see and correct. Duplicate rows that agree still carry."""
	by_id, by_key, ambiguous = {}, {}, set()
	for row in existing_rows:
		if not row.mapped_to_crm_field:
			continue
		if row.id:
			by_id[row.id] = row.mapped_to_crm_field
		if not row.key:
			continue
		if row.key in by_key and by_key[row.key] != row.mapped_to_crm_field:
			ambiguous.add(row.key)
		by_key[row.key] = row.mapped_to_crm_field
	for q in questions:
		mapping = by_id.get(q["id"]) or (by_key.get(q["key"]) if q["key"] not in ambiguous else None)
		if mapping:
			q["mapped_to_crm_field"] = mapping
