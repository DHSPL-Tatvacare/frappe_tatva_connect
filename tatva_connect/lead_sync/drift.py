"""Form drift: a Facebook form that was duplicated or recreated gets a NEW id, so a source still pointing
at the old one crawls a dead form — Graph answers 0 leads, nothing errors, and the leads simply stop.

The crawl asks Facebook which forms the Page actually has and says so out loud when the configured one is
not among them. Read only: it lists, it never stores a form and never touches a lead.
"""
import frappe
from frappe.utils.password import get_decrypted_password

from tatva_connect.lead_sync.discovery import list_forms
from tatva_connect.lead_sync.graph import redact_tokens

LOG_TYPE = "Unconfigured Form"


def report_form_drift(source) -> bool:
	"""True when the source's configured form is missing from its Page's live forms; logs it once per crawl.

	Never raises: a drift check that breaks the crawl is worse than the drift it reports."""
	try:
		page, token = _page_of(source.facebook_lead_form)
		if not (page and token):
			return False
		live = {f.get("id") for f in list_forms(page, token)}
		if not live or source.facebook_lead_form in live:
			return False
		_log(source, live)
		return True
	except Exception:
		frappe.log_error(
			title=f"Facebook form drift check failed: {source.name}",
			message=redact_tokens(frappe.get_traceback(with_context=True)),
		)
		return False


def _page_of(form_id: str) -> tuple[str | None, str | None]:
	"""The Page a form hangs off, and that Page's own token — the token the form listing needs. This app flips access_token to Password (fixtures/property_setter.json), so the column holds a mask and only the Auth row holds the secret."""
	page = frappe.db.get_value("Facebook Lead Form", form_id, "page")
	if not page:
		return None, None
	return page, get_decrypted_password("Facebook Page", page, "access_token", raise_exception=False)


def _log(source, live_ids: set) -> None:
	"""One Failed Lead Sync Log row naming the dead id and what Facebook does report, so the operator can repoint the source without reading a traceback."""
	frappe.get_doc({
		"doctype": "Failed Lead Sync Log",
		"type": LOG_TYPE,
		"source": source.name,
		"lead_data": frappe.as_json({
			"configured_form": source.facebook_lead_form,
			"live_forms": sorted(live_ids),
		}),
		"traceback": (
			f"Form {source.facebook_lead_form} is not among the lead forms Facebook reports for this Page. "
			"A duplicated or recreated form gets a new id — repoint this source at the live form."
		),
	}).insert(ignore_permissions=True)  # authz-ok: tier-a — scheduler lane, operator-visible log of their own source
	# The crawl rolls back on failure, and this row is the explanation of that failure — commit it or it dies with what it describes.
	frappe.db.commit()
