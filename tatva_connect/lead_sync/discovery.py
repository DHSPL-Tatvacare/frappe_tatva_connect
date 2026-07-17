"""Page + lead-form discovery: one unreadable Page never kills the readable ones."""
import frappe

from crm.lead_syncing.doctype.lead_sync_source.facebook import create_facebook_lead_form_in_db

from tatva_connect.lead_sync.graph import api_url, graph_get, redact_tokens


def fetch_and_store_pages(access_token: str) -> list[dict]:
	"""Replaces the upstream discovery; called from TatvaLeadSyncSource.before_insert."""
	if not access_token:
		frappe.throw(frappe._("Access token is required"))

	account_details = graph_get("token check (/me)", api_url("me"), {"access_token": access_token})
	if not account_details.get("id"):
		frappe.throw(frappe._("Invalid access token provided for Facebook."))

	pages = graph_get(
		"page listing (/me/accounts)",
		api_url("/me/accounts"),
		{"access_token": access_token},
	).get("data", [])

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
		if not frappe.db.exists("Facebook Page", page_id):
			_create_page(page, account_details)
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


def _create_page(page: dict, account_details: dict) -> None:
	frappe.get_doc(
		{
			"doctype": "Facebook Page",
			"page_name": page["name"],
			"id": page["id"],
			"category": page["category"],
			"access_token": page["access_token"],
			"account_id": account_details["id"],
		}
	).insert(ignore_permissions=True)  # authz-ok: tier-c — operator-driven discovery of their own Pages


def _fetch_and_store_forms(page_id: str, page_access_token: str) -> list[dict]:
	forms = graph_get(
		f"lead form listing for page {page_id}",
		api_url(f"/{page_id}/leadgen_forms"),
		{"access_token": page_access_token, "fields": "id,name,questions", "limit": 15000},
	).get("data", [])
	for form in forms:
		if not frappe.db.exists("Facebook Lead Form", form["id"]):
			create_facebook_lead_form_in_db(form, page_id)
	return forms
