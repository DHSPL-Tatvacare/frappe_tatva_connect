"""Desk-facing reads for the Facebook forms: the mappable catalog, the token verdict, and the refresh.

Read-only where it can be: the list of catalog field_keys a form's questions may map to, scoped to the
grain of the contract its Lead Sync Source is created against. The seam hands ingestion the SAME set —
so what an operator can pick and what a lead can actually carry are one answer, not two.
"""
import frappe
from frappe import _
from frappe.rate_limiter import rate_limit

from tatva_connect.lead import mapping
from tatva_connect.lead_sync import reconcile
from tatva_connect.lead_sync.discovery import fetch_and_store_pages
from tatva_connect.lead_sync.form import contract_for_form
from tatva_connect.lead_sync.token import app_for, expiry_date, inspect, page_of_form

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

	return [
		{"value": f["field_key"], "label": f"{f['label']} ({f['section']})", "section": f["section"]}
		for f in mapping.mappable_fields(contract=contract)
	]


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=10, seconds=60)
def validate_token(doctype: str, name: str) -> dict:
	"""Whether the stored credential would carry a crawl, asked of Graph rather than guessed.

	A verdict is returned and never the token itself, so the report can be read by anyone who may already
	write the record without handing them the secret to copy."""
	frappe.only_for("System Manager")
	if doctype not in ("Lead Sync Source", "Facebook Page", "CRM Facebook App"):
		frappe.throw(_("{0} carries no Facebook token.").format(doctype))
	frappe.has_permission(doctype, "write", doc=name, throw=True)

	doc = frappe.get_doc(doctype, name)
	token, origin = _credential(doc)
	report = {"ok": False, "checks": []}

	def check(label, passed, detail=""):
		report["checks"].append({"label": label, "passed": bool(passed), "detail": detail})
		return passed

	if not check("Credential", bool(token), origin):
		return report

	# Reported as a check rather than raised: a record saved before an app was named must still be readable.
	# An app record names no other app because it IS one, so the check is about the record that needs one.
	named = doc.doctype == "CRM Facebook App" or bool(doc.get("facebook_app"))
	if not check(
		"Facebook App named",
		named,
		doc.get("facebook_app") or doc.get("app_name")
		or "none named, so Facebook cannot be asked about the token",
	):
		return report

	app = app_for(doc)
	info, unreachable = inspect(token, app)
	if not check(
		"Accepted by Facebook",
		info.get("is_valid"),
		unreachable or ("accepted" if info.get("is_valid") else "Graph reports it as invalid or expired"),
	):
		return report

	check("Token kind", True, _token_kind(info))
	# A token issued by a DIFFERENT app cannot be exchanged or renewed here, and that is silent otherwise.
	issued_by = str(info.get("app_id") or "")
	check(
		"App",
		issued_by == str(app.app_id),
		f"{info.get('application') or 'unknown'} ({issued_by or '?'})"
		if issued_by == str(app.app_id)
		else f"issued by {info.get('application') or 'unknown'} ({issued_by or '?'}), but this record names {app.app_name} ({app.app_id})",
	)

	expiry = expiry_date(info)
	if expiry:
		days = frappe.utils.date_diff(expiry, frappe.utils.nowdate())
		# A token works until it lapses: today is a warning to act on, not a credential that has failed.
		check("Token expires", days >= 0, _detail(_when(expiry, days), None if days > 1 else _expiry_action(app)))
	else:
		check("Token expires", True, "never")

	# Separate from the token's own life: data access lapses at 90 days unless the person re-engages,
	# and a token that never expires still stops returning lead data on that date.
	data_access = expiry_date({"expires_at": info.get("data_access_expires_at")})
	if data_access:
		left = frappe.utils.date_diff(data_access, frappe.utils.nowdate())
		check("Data access expires", left > 7, _when(data_access, left))

	granted = set(info.get("scopes") or [])
	missing = [s for s in REQUIRED_SCOPES if s not in granted]
	check(
		"Permissions",
		not missing,
		f"all {len(REQUIRED_SCOPES)} granted" if not missing else f"missing: {', '.join(missing)}",
	)

	report["ok"] = all(c["passed"] for c in report["checks"])
	return report


def _detail(fact: str, action: "str | None" = None) -> str:
	"""A row reads as the value found, then at most one sentence to act on. One joiner, so no caller
	supplies the other's punctuation and no two rows are spaced differently."""
	return f"{fact}. {action}" if action else fact


def _when(date, days: int) -> str:
	"""One phrasing for every date in this report, so two rows never describe the same thing differently."""
	if days < 0:
		return f"{date} (expired)"
	if days == 0:
		return f"{date} (today)"
	if days == 1:
		return f"{date} (tomorrow)"
	return f"{date} ({days} days left)"


def _expiry_action(app) -> str:
	"""The one sentence an operator can act on, appended to the fact rather than replacing it.

	Time remaining cannot tell a freshly issued Explorer token from a sixty-day one at the end of its life,
	so this states what to do and never infers why. Earlier wording guessed a cause and sent operators to
	audit an App Secret that was correct."""
	if not app.secret():
		return f"Set the App Secret on {app.app_name}, then paste a fresh token."
	return "Paste a fresh token; Page tokens already discovered keep working."


def _token_kind(info: dict) -> str:
	"""Graph's own word for the token, plus whether a user token is the durable one."""
	kind = info.get("type") or "unknown"
	if kind != "USER":
		return kind
	return "USER, long-lived" if info.get("expires_at") else "USER, does not expire"


def _credential(doc) -> "tuple[str, str]":
	"""The credential this record is actually judged on, and where it came from.

	A Lead Sync Source is asked for `crawl_token()`, the SAME resolver the crawl calls, so the report can
	never pass a token the crawl would not use or fail one it would. Reading the record's own field here
	reported "nothing stored" for a healthy source whose Page token does the work."""
	if doc.doctype == "Lead Sync Source":
		token = doc.crawl_token()
		if not token:
			return "", "none. Refresh From Facebook to derive a Page token, or paste one above"
		_page, page_token = page_of_form(doc.facebook_lead_form) if doc.facebook_lead_form else (None, None)
		return token, "the Page token, which does not expire" if page_token else "the token on this record"
	token = doc.get_password("access_token", raise_exception=False) or ""
	if not token:
		# An absent credential must say so and name its remedy — the two holders are refilled differently.
		return "", ("none stored. Paste a user token from Graph API Explorer above"
		            if doc.doctype == "CRM Facebook App"
		            else "none stored. Refresh From Facebook to store this Page's own token")
	return token, "the token on this record"


def _discover(holder) -> dict:
	"""THE refresh: whichever record holds the token, discovery is the same act and the same answer.

	Discovery is app-wide — it re-reads every Page the token can see and every form on each — so the record
	it is launched from only decides which credential is used, never what is read."""
	pages = fetch_and_store_pages(
		holder.get_password("access_token", raise_exception=False), app_for(holder)
	)
	return {"pages": len(pages), "forms": sum(len(p.get("forms") or []) for p in pages)}


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=6, seconds=60)
def refresh_from_facebook(name: str) -> dict:
	"""Re-run discovery using a source's token. Kept for a site whose app holds no token of its own yet."""
	frappe.only_for("System Manager")
	frappe.has_permission("Lead Sync Source", "write", doc=name, throw=True)
	return _discover(frappe.get_doc("Lead Sync Source", name))


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=6, seconds=60)
def refresh_app(app: str) -> dict:
	"""Re-run discovery for one app — the door the Facebook Apps form and the Lead Forms list use.

	The app's own token is preferred. Where none is stored yet it falls back to an enabled source of that
	app, so a site configured before the app held a token keeps working untouched and starts using the
	app's token the moment one is pasted."""
	frappe.only_for("System Manager")
	frappe.has_permission("CRM Facebook App", "write", doc=app, throw=True)

	doc = frappe.get_doc("CRM Facebook App", app)
	if doc.get_password("access_token", raise_exception=False):
		return _discover(doc)

	source = frappe.db.get_value(
		"Lead Sync Source", {"facebook_app": app, "type": "Facebook", "enabled": 1}, "name"
	)
	if not source:
		frappe.throw(
			_("Paste an Access Token on {0} — there is no credential to reach Facebook with.").format(app),
			title=_("Access Token required"),
		)
	return _discover(frappe.get_doc("Lead Sync Source", source))


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=6, seconds=60)
def check_against_meta(facebook_lead_form: str, window: str = "7") -> dict:
	"""Queue a check of this form against Meta over `window`. Reads only; the report arrives by realtime when it finishes."""
	frappe.has_permission("Facebook Lead Form", "read", doc=facebook_lead_form, throw=True)
	frappe.has_permission("Lead Sync Source", "read", throw=True)
	if window not in reconcile.WINDOWS:
		frappe.throw(_("{0} is not a window this check offers.").format(window), title=_("Unknown window"))
	reconcile.start(facebook_lead_form, reconcile.WINDOWS[window])
	return {"queued": True, "window": window}


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=6, seconds=60)
def resync_missing(facebook_lead_form: str) -> dict:
	"""Queue the re-sync of everything the caller's last check left unlinked; the ids come from that check, never from the request."""
	frappe.has_permission("Facebook Lead Form", "read", doc=facebook_lead_form, throw=True)
	frappe.has_permission("Lead Sync Source", "write", throw=True)
	return reconcile.start_resync(facebook_lead_form)
