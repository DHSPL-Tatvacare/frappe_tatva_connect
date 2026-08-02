"""Page + lead-form discovery: one unreadable Page never kills the readable ones.

Two different things, two different rules. A PAGE is refreshed on every pass, because it holds the token
minted from whichever user token was current and a re-pasted credential has to reach the crawl. A FORM is
written once and never again: Meta's API has no update operation for a leadgen form, and a published form
cannot be edited in Ads Manager either — an edit is a duplicate, and a duplicate is a new id."""
import frappe

from tatva_connect.lead_sync.form import DISCOVERY_FLAG
from tatva_connect.lead_sync.graph import graph_get, redact_tokens

SWITCH_FORM_REFRESH = "Lead::Facebook::form-refresh"


def fetch_and_store_pages(access_token: str, app) -> list[dict]:
	"""Replaces the upstream discovery; called from TatvaLeadSyncSource.before_insert.

	`app` is the app that issued this token — it decides the Graph version every call here is made at,
	and it is stamped onto each Page as the provenance of the page token being stored."""
	if not access_token:
		frappe.throw(frappe._("Access token is required"))

	account_details = graph_get("token check (/me)", app.api_url("me"), {}, access_token)
	if not account_details.get("id"):
		frappe.throw(frappe._("Invalid access token provided for Facebook."))

	pages = graph_get("page listing (/me/accounts)", app.api_url("/me/accounts"), {}, access_token).get("data", [])

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
		_upsert_page(page, account_details, app)
		try:
			page["forms"] = _fetch_and_store_forms(page_id, page["access_token"], app)
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


def _upsert_page(page: dict, account_details: dict, app) -> None:
	"""Refresh, never skip: a Page that already exists still holds the token minted from the PREVIOUS user
	token, so a re-pasted credential would never reach the crawl.

	`facebook_app` is the provenance of the token being stored, not a claim that the app owns the Page —
	a Business owns Pages and Apps alike, and several apps may reach the same Page. It moves with the
	token: whichever app last minted this page token is the one that can describe or replace it."""
	values = {
		"page_name": page["name"],
		"category": page["category"],
		"access_token": page["access_token"],
		"account_id": account_details["id"],
		"facebook_app": app.name,
	}
	if frappe.db.exists("Facebook Page", page["id"]):
		doc = frappe.get_doc("Facebook Page", page["id"])
		doc.update(values)
		doc.save(ignore_permissions=True)  # authz-ok: tier-c — operator-driven discovery of their own Pages
		return
	frappe.get_doc({"doctype": "Facebook Page", "id": page["id"], **values}).insert(ignore_permissions=True)  # authz-ok: tier-c — operator-driven discovery of their own Pages


def list_forms(page_id: str, page_access_token: str, app) -> list[dict]:
	"""The live lead forms Facebook reports for a Page — READ ONLY, stores nothing. One lister, two callers: discovery (which then stores) and the crawl's drift check.
	`questions` already carries each question's `options`; the choice list is Facebook's own and is never retyped."""
	return graph_get(
		f"lead form listing for page {page_id}",
		app.api_url(f"/{page_id}/leadgen_forms"),
		{"fields": "id,name,questions{id,key,label,type,options}", "limit": 15000},
		page_access_token,
	).get("data", [])


def _fetch_and_store_forms(page_id: str, page_access_token: str, app) -> list[dict]:
	forms = list_forms(page_id, page_access_token, app)
	for form in forms:
		upsert_lead_form(form, page_id)
	return forms


def upsert_lead_form(form: dict, page_id: str) -> None:
	"""Store a form we have not seen. THE form writer: it lives here rather than in the fork because it
	decides what a question row holds, and that is a rule this app owns.

	A form ALREADY held is left exactly as it is, because a Facebook form cannot change. Meta's Marketing
	API offers no update operation on a leadgen form — create, read, and a status POST to archive or
	reactivate, nothing else — and a published form is locked in Ads Manager too: an edit is a duplicate,
	and a duplicate is a new form id. So a stored form's questions can never have gone stale, and
	rewriting all of them on every pass was a save per form per discovery for a change that cannot happen.
	The duplicate — the one way a form really does change — arrives here as a new id and is inserted
	below, with `_carry_mappings_from_page` bringing its siblings' mappings across."""
	if frappe.db.exists("Facebook Lead Form", form["id"]):
		return
	doc = frappe.get_doc(
		{"doctype": "Facebook Lead Form", "form_name": form["name"], "id": form["id"],
		 "page": page_id, "questions": _question_rows(form) or []}
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
	"""Nightly pass: every enabled Facebook source re-runs discovery, so a form published today — including
	the duplicate that a form "edit" really is — is visible tomorrow without a button press.

	Gated on its own operator switch like every other automation in this app: off, which is how it ships,
	the nightly pass does nothing and the forms stay as the last refresh left them.

	One unreadable source never stops the rest, and a source is skipped rather than repeated when its
	token has already been refreshed on this pass by a source that shares it."""
	from tatva_connect import automation
	from tatva_connect.lead_sync.token import app_for

	if not automation.is_enabled(SWITCH_FORM_REFRESH):
		return
	done_tokens = set()
	for name in frappe.get_all(
		"Lead Sync Source", filters={"type": "Facebook", "enabled": 1}, pluck="name"
	):
		source = frappe.get_doc("Lead Sync Source", name)
		token = source.get_password("access_token", raise_exception=False)
		# One token is one app's, so skipping a token already refreshed cannot skip a different app.
		if not token or token in done_tokens:
			continue
		done_tokens.add(token)
		try:
			fetch_and_store_pages(token, app_for(source))
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

	Duplicating is the ONLY way a published form gets edited, and the copy arrives with a new id and EVERY
	question unmapped — including the one holding the phone number, without which every lead from that form
	fails to upsert and is logged rather than stored. So this is the whole of the mapping-carry story: there
	is no within-a-form carry, because a form's questions never change under its own id.

	Matched on `key`, which Facebook derives from the question text, so the same question carries the same
	key into the copy. A key mapped DIFFERENTLY on two siblings identifies nothing and carries nothing,
	rather than applying one operator decision to a question it was never made for."""
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
