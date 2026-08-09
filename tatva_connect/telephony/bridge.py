"""The outbound core. `make_a_call` is called directly by CallUI.vue; `get_call_log` is the one hooks override left, for the recording player."""
from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import cint

from tatva_connect import phone as phone_utils
from tatva_connect.telephony import providers, routing, writer
from tatva_connect.utils import spend_rate_limit

MEDIUM = "Acefone"
RECORDING_ENDPOINT = "/api/method/tatva_connect.api.telephony.recording"

SETTINGS = "CRM Telephony Settings"
# Per-minute cap, at the account level because that is where Acefone enforces its own. Operator data;
# blank or zero means the default, never "no calls allowed".
_PER_MINUTE_FIELD = "calls_per_minute_per_account"
_DEFAULT_PER_MINUTE = 60
# Long enough to eat a double-click, short enough not to block a genuine redial.
_REPEAT_SECONDS = 5


@frappe.whitelist()
def make_a_call(to_number, from_number=None, caller_id=None):
	"""Place a bridge call: the caller passes only a number, and the lead, account, provider and agent line are resolved from it."""
	ref_doctype, ref_name = _reference_for_number(to_number)
	# The row is minted with ignore_permissions, so gate it on READ of the parent — no calling a lead you cannot see.
	if ref_name:
		frappe.has_permission(ref_doctype, "read", ref_name, throw=True)
	account_name = routing.resolve_for_reference(ref_doctype, ref_name) if ref_name else None
	if not account_name:
		frappe.throw(_("No telephony account route for this number — configure Telephony Routing."))

	account = frappe.get_cached_doc("CRM Telephony Account", account_name)
	adapter = providers.adapter_for(account)
	adapter.assert_enabled()
	agent_number = _agent_number(account)
	_throttle(account_name, to_number)  # before the row is minted: a refusal must leave no Initiated row
	# caller passed, never defaulted: the same writer serves automation, which must not record a session user.
	call_log = _new_call_log(
		to_number, agent_number, account_name, ref_doctype, ref_name, providers.provider_of(account),
		caller=frappe.session.user,
	)

	resp = adapter.click_to_call(
		account,
		destination_number=to_number,
		agent_number=agent_number,
		caller_id=account.caller_id,
		custom_identifier=call_log.get(writer.CALL_KEY_FIELD),  # belt-and-braces; `ref_id` is the real key
	)
	if not adapter.succeeded(resp):
		_refuse(call_log, resp, adapter, providers.provider_of(account))
	_adopt_ref_id(call_log, resp)
	return {"name": call_log.name}


def _refuse(call_log, resp, adapter, provider) -> None:
	"""Mark the row Failed DURABLY then raise; frappe.throw rolls back, so an uncommitted status is lost."""
	call_log.db_set("status", "Failed")
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


def _throttle(account_name, to_number) -> None:
	"""Refuse an originate the line's minute, or a double-click, has already spent."""
	user = frappe.session.user
	spend_rate_limit(
		"telephony-rl:account", account_name, _cap(_PER_MINUTE_FIELD, _DEFAULT_PER_MINUTE), 60,
		_("This telephony line is at its call limit for the minute. Try again shortly."),
	)

	digits = phone_utils.match_digits(to_number, last=10) or str(to_number)
	repeat = frappe.cache.make_key(f"telephony-repeat:{user}:{digits}")
	# setnx, so redis decides the winner rather than two requests both reading "absent".
	if not frappe.cache.setnx(repeat, 1):
		frappe.throw(_("That call is already being placed."), exc=frappe.RateLimitExceededError)
	frappe.cache.expire(repeat, _REPEAT_SECONDS)


def _reference_for_number(number):
	"""Resolve a phone number to its lead/deal (no auto-create)."""
	from crm.integrations.api import get_contact_by_phone_number

	contact = get_contact_by_phone_number(str(number)) or {}
	if contact.get("lead"):
		return "CRM Lead", contact["lead"]
	if contact.get("deal"):
		return "CRM Deal", contact["deal"]
	return None, None


def _agent_number(account):
	"""Caller's own Acefone line if set, else the account's default."""
	number = frappe.db.get_value(
		"CRM Telephony Agent", {"user": frappe.session.user}, "acefone_number"
	) or account.agent_number
	if not number:
		frappe.throw(_("No telephony agent number set for you or the account."))
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
	"""crm's get_call_log, plus a playable path for Acefone recordings.

	Delegates to the original (Twilio/Exotel untouched), then for an Acefone call
	with a recording, points `recording_url_path` at our streaming proxy so the
	native inline "Listen" player works — no audio stored.
	"""
	from crm.fcrm.doctype.crm_call_log.crm_call_log import get_call_log as crm_get_call_log

	frappe.has_permission("CRM Call Log", "read", name, throw=True)
	data = crm_get_call_log(name)
	# crm always points recording_url_path at its own (Twilio/Exotel-only) proxy,
	# so for an Acefone call we OVERWRITE it with our streaming proxy.
	if data.get("recording_url") and frappe.db.get_value("CRM Call Log", name, "telephony_medium") == MEDIUM:
		data["recording_url_path"] = f"{RECORDING_ENDPOINT}?call_log={quote(name)}"
	return data
