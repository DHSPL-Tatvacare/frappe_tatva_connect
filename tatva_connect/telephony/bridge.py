"""The outbound core. `make_a_call` is called directly by CallUI.vue; `get_call_log` is the one hooks override left, for the recording player."""
from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, now_datetime

from tatva_connect import phone
from tatva_connect.telephony import cache, providers, resolve, routing, writer
from tatva_connect.utils import spend_rate_limit

MEDIUM = "Acefone"
RECORDING_ENDPOINT = "/api/method/tatva_connect.api.telephony.recording"

SETTINGS = "CRM Telephony Settings"
# Per-minute cap, at the account level because that is where Acefone enforces its own. Operator data;
# blank or zero means the default, never "no calls allowed".
_PER_MINUTE_FIELD = "calls_per_minute_per_account"
_DEFAULT_PER_MINUTE = 60


@frappe.whitelist()
def call_context(reference_doctype, reference_name):
	"""What the call modal shows before dialling: the rep's extension on this record's account, and the numbers its grain calls from."""
	rule, account = _route(reference_doctype, reference_name)
	numbers = _caller_pool(rule, account)
	own = phone.match_digits(account.caller_id, last=10)
	return {
		"account": account.name,
		"extension": _agent_number(account),
		"dids": numbers,
		"default_did": _default_caller_id(numbers, own),
	}


@frappe.whitelist()
def make_a_call(to_number, reference_doctype, reference_name, caller_id=None):
	"""Place a bridge call for the record on screen: its grain picks the account, the DID and the rep's extension."""
	rule, account = _route(reference_doctype, reference_name)
	account_name = account.name
	adapter = providers.adapter_for(account)
	adapter.assert_enabled()
	agent_number = _agent_number(account)
	_assert_free(reference_doctype, reference_name)
	_throttle(account_name)  # before the row is minted: a refusal must leave no Initiated row
	# caller passed, never defaulted: the same writer serves automation, which must not record a session user.
	call_log = _new_call_log(
		to_number, agent_number, account_name, reference_doctype, reference_name, providers.provider_of(account),
		caller=frappe.session.user,
	)

	resp = adapter.click_to_call(
		account,
		destination_number=to_number,
		agent_number=agent_number,
		caller_id=_caller_id(rule, account, caller_id),
		custom_identifier=call_log.get(writer.CALL_KEY_FIELD),  # belt-and-braces; `ref_id` is the real key
	)
	if not adapter.succeeded(resp):
		_refuse(call_log, resp, adapter, providers.provider_of(account))
	_adopt_ref_id(call_log, resp)
	return {"name": call_log.name}


def _refuse(call_log, resp, adapter, provider) -> None:
	"""Mark the row Failed and log the provider's answer, committed before the throw rolls back."""
	call_log.db_set("status", "Failed")
	frappe.log_error(
		title=f"telephony: {provider} refused click-to-call",
		message=frappe.as_json({"account": call_log.get("custom_telephony_account"), "response": resp}),
		reference_doctype=call_log.doctype,
		reference_name=call_log.name,
	)
	frappe.db.commit()

	if adapter.token_rejected(resp):
		frappe.throw(
			_("{0} rejected our credentials — the account's API token is expired or invalid.").format(provider),
			title=_("Call Failed"),
		)

	if (resp or {}).get("status_code") == 429:
		# Never echo the gateway body; frappe maps this to HTTP 429 so our cap and theirs arrive alike.
		wait = (resp or {}).get("retry_after")
		frappe.throw(
			_("{0} is busy — try again in {1} seconds.").format(provider, wait)
			if wait
			else _("{0} is busy — try again in a moment.").format(provider),
			exc=frappe.RateLimitExceededError,
			title=_("Call Failed"),
		)

	info = (resp or {}).get("message") or _("click-to-call was rejected")
	frappe.throw(
		_("{0} could not place the call: {1}").format(provider, info),
		title=_("Call Failed"),
	)


def _adopt_ref_id(call_log, resp) -> None:
	"""Take Acefone's `ref_id` over our placeholder so the CDR finds this row by key, not by recency."""
	ref_id = str((resp or {}).get("ref_id") or "").strip()
	if not ref_id:
		return
	call_log.db_set(writer.CALL_KEY_FIELD, ref_id)
	frappe.db.commit()


def _cap(field, fallback):
	"""An operator-tunable per-minute cap; an unreadable setting falls back rather than block calling."""
	try:
		return cint(frappe.db.get_single_value(SETTINGS, field)) or fallback
	except Exception:
		return fallback


def _throttle(account_name) -> None:
	"""Refuse an originate the line has already spent its minute on."""
	spend_rate_limit(
		"telephony-rl:account", account_name, _cap(_PER_MINUTE_FIELD, _DEFAULT_PER_MINUTE), 60,
		_("This telephony line is at its call limit for the minute. Try again shortly."),
	)


def _assert_free(ref_doctype, ref_name) -> None:
	"""Refuse a second call to a record someone is already ringing — one rep double-clicking, or two reps on one lead.

	The Call Log row IS that state, so it is read rather than mirrored into a cache key that can disagree with it.
	`OUTBOUND_MATCH_WINDOW_MIN` already means "this outbound call is still in flight" — the same window the writer
	matches a CDR on — so a row the provider never reported back stops blocking on its own.
	"""
	if not ref_name:
		return
	cutoff = add_to_date(now_datetime(), minutes=-writer.OUTBOUND_MATCH_WINDOW_MIN)
	rows = frappe.get_all(
		"CRM Call Log",
		filters={
			"reference_doctype": ref_doctype,
			"reference_docname": ref_name,
			"type": "Outgoing",
			"status": "Initiated",
			"creation": [">=", cutoff],
		},
		fields=["caller"],
		limit=1,
		# authz-ok: tier-b — gated: the caller passed has_permission on this very record above
		ignore_permissions=True,
	)
	if not rows:
		return
	caller = rows[0].get("caller")
	who = frappe.get_cached_value("User", caller, "full_name") if caller else None
	frappe.throw(
		_("{0} is already calling this record.").format(who)
		if who
		else _("A call to this record is already being placed."),
		exc=frappe.RateLimitExceededError,
		title=_("Call In Progress"),
	)


def _route(reference_doctype, reference_name):
	"""The routing rule and account the record on screen calls through; refused when its grain has no rule."""
	# The row is minted with ignore_permissions, so gate it on READ of the record — no calling a lead you cannot see.
	frappe.has_permission(reference_doctype, "read", reference_name, throw=True)
	rule = routing.resolve_rule_for_reference(reference_doctype, reference_name)
	if not rule:
		frappe.throw(
			_("{0} is not on any telephony route — configure Telephony Routing for its product line, group and programme.").format(reference_name),
			title=_("Call Failed"),
		)
	return rule, frappe.get_cached_doc("CRM Telephony Account", rule.telephony_account)


def _caller_pool(rule, account):
	"""The numbers this grain may show the patient: its own enabled DIDs, or the account's Caller ID where it lists none."""
	def build():
		rows = frappe.get_all(
			resolve.DID_CHILD,
			filters={"parenttype": resolve.ROUTING_DOCTYPE, "parent": rule.name, "enabled": 1},
			fields=["did_number", "label", "telephony_account"],
			order_by="idx asc",
		)
		mine = [
			{"did_number": r.did_number, "label": r.label}
			for r in rows
			if r.telephony_account in (None, "", rule.telephony_account)
		]
		own = phone.match_digits(account.caller_id, last=10)
		return mine or ([{"did_number": own, "label": _("Account caller ID")}] if own else [])

	return [frappe._dict(row) for row in cache.read("pool", rule.name, build)]


def _default_caller_id(numbers, own):
	"""The number a grain shows unless the rep picks another: the account's Caller ID where the grain lists it, else its first."""
	return next((n.did_number for n in numbers if n.did_number == own), numbers[0].did_number if numbers else None)


def _caller_id(rule, account, picked):
	"""The number the patient sees: one this grain calls from, never another grain's; unpicked means the grain's default."""
	numbers = _caller_pool(rule, account)
	digits = phone.match_digits(picked, last=10) or _default_caller_id(numbers, phone.match_digits(account.caller_id, last=10))
	if not any(n.did_number == digits for n in numbers):
		frappe.throw(_("{0} is not a number this grain calls from.").format(picked or _("(none)")), title=_("Call Failed"))
	return digits


def _agent_number(account):
	"""The caller's extension on this account, else the account's callback number."""
	number = resolve.seat_for_user(frappe.session.user, account.name) or account.agent_number
	if not number:
		frappe.throw(
			_("You have no extension on {0}. Ask an administrator to add it under Telephony Agents.").format(account.name),
			title=_("Call Failed"),
		)
	return number


def _new_call_log(to_number, agent_number, account_name, ref_doctype, ref_name, medium,
                  call_id=None, caller=None, account_field="custom_telephony_account"):
	"""THE ONE WRITER for an outbound row, rep or automation; `call_id` set only when the provider's id is known at placement, and such a row claims NO provider-key column."""
	doc = frappe.new_doc("CRM Call Log")
	doc.id = call_id or frappe.generate_hash(length=12)
	if not call_id:
		setattr(doc, writer.CALL_KEY_FIELD, doc.id)
	doc.type = "Outgoing"
	doc.status = "Initiated"
	doc.telephony_medium = medium
	if account_name and account_field:
		setattr(doc, account_field, account_name)
	setattr(doc, "from", str(agent_number or ""))
	doc.to = str(to_number)
	doc.caller = caller
	if ref_name:
		doc.reference_doctype = ref_doctype
		doc.reference_docname = ref_name
		doc.link_with_reference_doc(ref_doctype, ref_name)
	doc.insert(ignore_permissions=True)  # authz-ok: tier-b — webhook: token-authenticated before the write
	frappe.db.commit()
	return doc


@frappe.whitelist()
def get_call_log(name):
	"""crm's get_call_log, plus a playable path for a recording this app never stored.

	Delegates to the original (Twilio/Exotel untouched). A call whose audio IS ours needs nothing here:
	the row points at our own file and the screen reads it through `call_media.media_for`, which is what
	every stored recording — telephony and AI voice alike — plays from.

	What is left is the LEGACY row: logged before the bytes were fetched, still carrying the provider's own
	absolute URL, which is never put in the markup. Those keep the permission-gated streaming proxy, and
	they stop needing it the moment the backfill adopts them.
	"""
	from crm.fcrm.doctype.crm_call_log.crm_call_log import get_call_log as crm_get_call_log

	frappe.has_permission("CRM Call Log", "read", name, throw=True)
	data = crm_get_call_log(name)
	# `http`-prefixed means the row still holds the PROVIDER's URL; our own copy is a same-origin file path.
	legacy = str(data.get("recording_url") or "").startswith("http")
	if legacy and frappe.db.get_value("CRM Call Log", name, "telephony_medium") == MEDIUM:
		data["recording_url_path"] = f"{RECORDING_ENDPOINT}?call_log={quote(name)}"
	return data
