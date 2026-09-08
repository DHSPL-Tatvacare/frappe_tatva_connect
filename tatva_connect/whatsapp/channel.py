"""The WhatsApp CHANNEL — everything true of WhatsApp whoever carries it.

A switch is a property of the channel, not of the vendor behind it. `WhatsApp::WATI::messaging` said
otherwise, and it meant an operator who changed provider would silently lose their own configuration:
the new vendor's key defaults OFF, so WhatsApp would go dark on migrate with nothing in the log to say
why. The keys are `WhatsApp::Channel::messaging|templates|reconcile` — `Channel` is the subject because
they gate the whole channel, not one vendor — and the per-vendor control is the one that genuinely
belongs to a vendor: an account's Active/Inactive status.

Address format is the channel's too. E.164 digits is how WhatsApp names a subscriber; that is not a
WATI convention and a second provider will not spell it differently.
"""
import frappe
from frappe import _

from tatva_connect import automation

CHANNEL = "whatsapp"

# The operator toggles. Vendor-free by construction — see the module docstring.
SWITCH_MESSAGING = "WhatsApp::Channel::messaging"
SWITCH_TEMPLATES = "WhatsApp::Channel::templates"
SWITCH_RECONCILE = "WhatsApp::Channel::reconcile"
SWITCH_RECOVERY = "WhatsApp::Channel::recovery"
SWITCH_MEDIA_RETRY = "WhatsApp::Channel::media-retry"
SWITCH_ENROLMENT = "WhatsApp::Channel::enrolment"


def id_space(account) -> list[str]:
	"""Every account whose provider message ids share one space with this one — itself included.

	A provider mints ids per TENANT, and one tenant can carry many numbers, which this app models as one
	account row per number. So two accounts on one tenant share an id space, and a receipt or an echo for
	a message sent on either can be delivered on the other's webhook: the sent/status events carry no
	channel field at all (measured — only inbound `message` names one), so the URL token is the only
	thing that says which row received them, and it says the sibling.

	Read as the ACCOUNT scope, that receipt is about a message this account never sent, so the message was
	filed a second time onto whichever lead the sibling routes to — one patient's reply to a Liver Forever
	message written onto a GoodFlip Support lead. Read as the TENANT scope, it is what it is: a receipt for
	a message we hold.

	The tenant is the account's own `url` — the provider base every one of its numbers is reached through,
	which for WATI carries the tenant id. Compared with the trailing slash removed, because an operator
	pasting one is the defect `transport.base_url` already exists to absorb.

	An account with no url shares nothing and answers with itself, which is exactly the old behaviour.
	"""
	url = (frappe.get_cached_value("WhatsApp Account", account, "url") or "").rstrip("/")
	if not url:
		return [account]
	shared = [
		row.name
		for row in frappe.get_all("WhatsApp Account", fields=["name", "url"])
		if (row.url or "").rstrip("/") == url
	]
	return shared or [account]


def is_enabled() -> bool:
	"""The WhatsApp master kill-switch. Dormant by default — OFF until explicitly enabled (a blank or
	unsaved single reads as disabled). It stops send AND receive."""
	return automation.is_enabled(SWITCH_MESSAGING)


def assert_enabled():
	"""Block sends while the kill-switch is off."""
	if not is_enabled():
		frappe.throw(
			_("WhatsApp is switched off (CRM Tatva Automation → {0}). No messages are sent.").format(
				SWITCH_MESSAGING
			),
			title=_("WhatsApp disabled"),
		)


def screen_send(adapter, to, reference_doctype=None, reference_name=None):
	"""THE GATE. Every outbound WhatsApp message passes here or it is not sent.

	Returns `(number, refusal)` — exactly one is ever set. `number` is the recipient spelled the way THIS
	provider requires it; `refusal` is one plain-English sentence saying why nothing will be sent.

	It judges two things, and it is the only place either is judged:

	  * CONSENT, which is a fact about a PERSON. `custom_whatsapp_opt_out` had no reader anywhere in the
	    send path, so 106 of this bench's 2,381 leads could be messaged after saying no — by a workflow,
	    by a rep, by a notification, identically.
	  * THE ADDRESS, via `adapter.DECLARATION.conform_number` — Chunk 1's mechanism, asked, never
	    reimplemented. A number the provider would have to guess the country of is refused, which is the
	    defect that sent a patient's message to a different subscriber in another country.

	A send that names NO LEAD is refused. Consent belongs to a person, so a send to a naked number is a
	send to somebody whose consent nobody established. No phone-number lookup rescues it: a number can
	match several leads and there is no single consent answer to read off it, so guessing one would be
	the inference this gate exists to delete. The only surface that sends without a lead is the bulk
	doctype, which has no rows and no recipient lists.

	Callers turn ONE refusal into their own idiom, which is the only thing that legitimately differs: a
	workflow returns it as a routable `failed` outcome, a rep's manual send throws so the rep is told,
	a notification logs it. None of them re-decides whether to send.
	"""
	if reference_doctype != "CRM Lead" or not reference_name:
		return None, _("this send names no lead, so the patient's consent cannot be established")
	if frappe.db.get_value("CRM Lead", reference_name, "custom_whatsapp_opt_out"):
		return None, _("the patient on lead {0} has opted out of WhatsApp").format(reference_name)
	number = adapter.DECLARATION.conform_number(to)
	if not number:
		return None, _("the number {0} is not one {1} can dial - it requires {2}").format(
			to, adapter.DECLARATION.provider, adapter.DECLARATION.number_format
		)
	return number, None
