"""Desk-facing reads for the Facebook forms: the mappable catalog, the token verdict, and the refresh.

Read-only where it can be: the list of catalog field_keys a form's questions may map to, scoped to the
grain of the contract its Lead Sync Source is created against. The SAME set `allowed_field_keys` hands
ingestion — so what an operator can pick and what a lead can actually carry are one answer, not two.
"""
import frappe
from frappe import _
from frappe.rate_limiter import rate_limit

from tatva_connect.lead_sync.contract import allowed_field_keys
from tatva_connect.lead_sync.discovery import fetch_and_store_pages
from tatva_connect.lead_sync.form import contract_for_form
from tatva_connect.lead_sync.token import app_credentials, expiry_date, page_of_form, token_info

# The scopes a crawl cannot run without; reported one by one so a missing grant names itself.
REQUIRED_SCOPES = (
	"leads_retrieval",
	"pages_show_list",
	"business_management",
	"pages_read_engagement",
	"ads_management",
)


@frappe.whitelist()
def list_mappable_fields(facebook_lead_form):
	"""[{value: field_key, label, section}] for the question picker; [] when no source/contract yet.

	Gated on read of Lead Sync Source (System-Manager-only), so this surfaces the catalog to builders only.
	"""
	frappe.has_permission("Lead Sync Source", "read", throw=True)

	contract = contract_for_form(facebook_lead_form)
	if not contract:
		return []

	keys = allowed_field_keys(contract)
	if not keys:
		return []

	rows = frappe.get_all(
		"CRM Lead API Field",
		filters={"field_key": ("in", list(keys))},
		fields=["field_key", "label", "section"],
		order_by="section asc, label asc",
	)
	return [
		{"value": r.field_key, "label": f"{r.label or r.field_key} ({r.section})", "section": r.section}
		for r in rows
	]


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=10, seconds=60)
def validate_token(doctype: str, name: str) -> dict:
	"""Whether the stored credential would carry a crawl, asked of Graph rather than guessed.

	A verdict is returned and never the token itself, so the report can be read by anyone who may already
	write the record without handing them the secret to copy."""
	frappe.only_for("System Manager")
	if doctype not in ("Lead Sync Source", "Facebook Page"):
		frappe.throw(_("{0} carries no Facebook token.").format(doctype))
	frappe.has_permission(doctype, "write", doc=name, throw=True)

	token = frappe.get_doc(doctype, name).get_password("access_token", raise_exception=False)
	report = {"ok": False, "checks": []}

	def check(label, passed, detail=""):
		report["checks"].append({"label": label, "passed": bool(passed), "detail": detail})
		return passed

	if not check("Token stored", bool(token), "yes" if token else "nothing is stored on this record"):
		return report

	info = token_info(token)
	if not check(
		"Accepted by Facebook",
		info.get("is_valid"),
		"yes" if info.get("is_valid") else "Graph reports it as invalid or expired",
	):
		return report

	check("Token kind", True, _token_kind(info))
	check("App", bool(info.get("app_id")), f"{info.get('application') or 'unknown'} ({info.get('app_id') or '?'})")

	expiry = expiry_date(info)
	if expiry:
		days = frappe.utils.date_diff(expiry, frappe.utils.nowdate())
		check("Token expires", days > 1, f"{expiry} ({days} days left)" if days > 1 else _short_token_reason())
	else:
		check("Token expires", True, "never")

	# Separate from the token's own life: data access lapses at 90 days unless the person re-engages,
	# and a token that never expires still stops returning lead data on that date.
	data_access = expiry_date({"expires_at": info.get("data_access_expires_at")})
	if data_access:
		left = frappe.utils.date_diff(data_access, frappe.utils.nowdate())
		check("Data access expires", left > 7, f"{data_access} ({left} days left)")

	granted = set(info.get("scopes") or [])
	missing = [s for s in REQUIRED_SCOPES if s not in granted]
	check(
		"Permissions",
		not missing,
		f"all {len(REQUIRED_SCOPES)} granted" if not missing else f"missing: {', '.join(missing)}",
	)

	if doctype == "Lead Sync Source":
		_check_crawl_credential(frappe.get_doc(doctype, name), check)

	report["ok"] = all(c["passed"] for c in report["checks"])
	return report


def _short_token_reason() -> str:
	"""Why a token is still short-lived, which is a different instruction depending on the app credentials.
	Reporting "set the App Secret" when it is already set sends an operator to look at the wrong thing."""
	app_id, app_secret = app_credentials()
	if not (app_id and app_secret):
		return "Short-lived. Set App ID and App Secret in Facebook Settings, then save this source again."
	return (
		"Short-lived, and the exchange did not replace it. The App Secret is set, so it is likely wrong "
		"or belongs to a different app. Check the Error Log for the exchange failure."
	)


def _token_kind(info: dict) -> str:
	"""Graph's own word for the token, plus whether a user token is the durable one."""
	kind = info.get("type") or "unknown"
	if kind != "USER":
		return kind
	return "USER, long-lived" if info.get("expires_at") else "USER, does not expire"


def _check_crawl_credential(source, check) -> None:
	"""The crawl runs on the Page token, so a source whose Page is undiscovered is reported as such."""
	if not source.facebook_lead_form:
		return
	_page, page_token = page_of_form(source.facebook_lead_form)
	check(
		"Crawl runs on the Page token",
		bool(page_token),
		"" if page_token else "No Page token stored yet. Refresh from Facebook to derive one.",
	)


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=6, seconds=60)
def refresh_from_facebook(name: str) -> dict:
	"""Re-run discovery for a source: Pages, their tokens, and every form's questions are brought current.

	The same call the nightly job makes, so a button press and a scheduled pass cannot drift apart."""
	frappe.only_for("System Manager")
	frappe.has_permission("Lead Sync Source", "write", doc=name, throw=True)

	source = frappe.get_doc("Lead Sync Source", name)
	pages = fetch_and_store_pages(source.get_password("access_token", raise_exception=False))
	return {"pages": len(pages), "forms": sum(len(p.get("forms") or []) for p in pages)}
