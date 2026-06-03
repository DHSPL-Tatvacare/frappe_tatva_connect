"""Make Acefone ride crm's NATIVE call UI via clean overrides — no crm fork.

crm's call UI (the phone icon on lead/deal/contact, "Make a Call", the call
popup, the inline "Listen" recording player) turns on when a telephony
integration's settings are enabled. We enable the Exotel slot (no Exotel creds
needed) and override the two backend methods that UI calls, so the native UI
drives Acefone instead. The CRM never reaches Exotel — make_a_call is replaced.

Registered in hooks.override_whitelisted_methods:
  crm.integrations.exotel.handler.make_a_call          -> make_a_call
  crm.fcrm.doctype.crm_call_log.crm_call_log.get_call_log -> get_call_log
"""
from urllib.parse import quote

import frappe
from frappe import _

from tatva_connect.acefone import api as acefone
from tatva_connect.acefone import routing

MEDIUM = "Acefone"
RECORDING_ENDPOINT = "/api/method/tatva_connect.api.telephony.recording"


@frappe.whitelist()
def make_a_call(to_number, from_number=None, caller_id=None):
	"""Place an Acefone bridge call — drop-in for crm's Exotel make_a_call.

	The native UI passes only the number, so we resolve the lead/deal and its
	routed account from it. Returns the new call-log name (the popup just shows
	"Calling…"); failures raise so the native toast shows a clean message.
	"""
	acefone.assert_enabled()

	ref_doctype, ref_name = _reference_for_number(to_number)
	account_name = routing.resolve_for_reference(ref_doctype, ref_name) if ref_name else None
	if not account_name:
		frappe.throw(_("No Acefone account route for this number — configure Acefone Account Routing."))

	account = frappe.get_cached_doc("Acefone Account", account_name)
	agent_number = _agent_number(account)
	call_log = _new_call_log(to_number, agent_number, account_name, ref_doctype, ref_name)

	resp = acefone.click_to_call(
		account,
		destination_number=to_number,
		agent_number=agent_number,
		caller_id=account.caller_id,
		custom_identifier=call_log.name,
	)
	if not (resp or {}).get("success"):
		call_log.db_set("status", "Failed")
		info = (resp or {}).get("message") or _("click-to-call was rejected")
		frappe.throw(_("Acefone could not place the call: {0}").format(info), title=_("Call Failed"))
	return {"name": call_log.name}


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
		frappe.throw(_("No Acefone agent number set for you or the account."))
	return number


def _new_call_log(to_number, agent_number, account_name, ref_doctype, ref_name):
	doc = frappe.new_doc("CRM Call Log")
	doc.id = frappe.generate_hash(length=12)
	doc.type = "Outgoing"
	doc.status = "Initiated"
	doc.telephony_medium = MEDIUM
	doc.custom_acefone_account = account_name
	setattr(doc, "from", str(agent_number))
	doc.to = str(to_number)
	doc.caller = frappe.session.user
	if ref_name:
		doc.reference_doctype = ref_doctype
		doc.reference_docname = ref_name
		doc.link_with_reference_doc(ref_doctype, ref_name)
	doc.insert(ignore_permissions=True)
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

	data = crm_get_call_log(name)
	# crm always points recording_url_path at its own (Twilio/Exotel-only) proxy,
	# so for an Acefone call we OVERWRITE it with our streaming proxy.
	if data.get("recording_url") and frappe.db.get_value("CRM Call Log", name, "telephony_medium") == MEDIUM:
		data["recording_url_path"] = f"{RECORDING_ENDPOINT}?call_log={quote(name)}"
	return data
