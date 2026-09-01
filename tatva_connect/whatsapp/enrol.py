"""Lead enrolment from an inbound WhatsApp message — the channel as a FIFTH source of leads.

`ingest._targets` is strict on purpose: a message whose number matches no lead routing to the receiving
account is dropped rather than guessed onto somebody else's record. That strictness is right, and it is
also why a stranger who writes to a programme's number is invisible — there is no lead, so there is no
row, so nothing downstream ever sees them.

This is the answer to that, and it is deliberately NOT a second lead-create implementation. `intake`,
the Facebook sync and the Desk import all resolve their own payload and then hand it to the ONE brain,
`partner._upsert_one`, which owns find-or-create, dedup on phone, forced routing, program resolution and
the race against its own unique index. This module keeps only what is genuinely WhatsApp's: what a
message tells us about a person (a number, and whatever name they set on their profile), and which
grain to file them under.

THE GRAIN IS READ, NOT CONFIGURED. It comes from `routing.grain_for_account` — the routing rules run
backwards. An operator typing the grain into a second place could disagree with the rules, and a lead
created at a grain that resolves to a different account is an orphan: the very message that created it
still cannot attach to it. Reading the rules makes the round trip true by construction.

DORMANT, TWICE. The channel switch must be armed AND the receiving account must be individually ticked,
so arming this for one programme can never mint leads on another's number.
"""
import frappe

from tatva_connect import automation
from tatva_connect.whatsapp import channel, routing

# What a lead born this way is sourced as. `CRM Lead Source` autonames on the value, so the row IS the
# string; the master is picked, never invented here (`_upsert_one` validates the Link on save).
SOURCE = "WhatsApp"

# The per-account tick. An account-level property, not a rule-level one: it answers "does this number
# accept strangers", which is a fact about the number and not about any one grain that reaches it.
ACCOUNT_FLAG = "custom_create_lead_on_unknown_sender"


def is_enabled(account) -> bool:
	"""Both gates, asked in the order that costs least. The switch is a cached single read and settles
	the whole site; the account flag is a row read that only a site with the switch armed ever pays."""
	if not automation.is_enabled(channel.SWITCH_ENROLMENT):
		return False
	return bool(frappe.db.get_value("WhatsApp Account", account, ACCOUNT_FLAG))


def lead_for_event(event):
	"""The lead this inbound event belongs to, creating one if the sender is a stranger — or None.

	Returns None for every reason a lead must NOT be born: the gates are shut, the account declares no
	grain, or the event carries no number to identify a person by. None is the caller's signal to do
	exactly what it does today, which is drop the message with a log line.

	Never raises. Enrolment is an ADDITION to a path whose existing outcome is already "drop"; a fault
	here must cost the new behaviour, never the old one, and the webhook spine keeps the delivery
	replayable either way.
	"""
	number = event.subject_number
	if not (number and event.account):
		return None
	if not is_enabled(event.account):
		return None
	grain = routing.grain_for_account(event.account)
	if grain is None:
		# The account is ticked but its rules do not describe one catchment. Silence here would look
		# exactly like a sender who simply was not new, so it is said out loud where an operator reads it.
		frappe.log_error(
			title="whatsapp enrolment skipped: the account declares no single grain",
			message=f"account={event.account} — add a routing rule, or make the broadest one unambiguous",
		)
		return None
	# A SAVEPOINT, not a bare try/except. `propagate.py` names the shape and why: if the failure came out
	# of the database — a deadlock, an index, a truncated column — the transaction is ALREADY poisoned, so
	# swallowing the exception only moves the crash onto the next statement, which here is the insert of
	# the patient's own message. The mark/rollback/release trio is frappe's, in the order `sends.py`
	# already uses it for the same reason.
	#
	# `propagate.fail_safe` itself is not reused: it is a doc_event decorator (doc, method) and this is
	# not a hook. Its `_must_surface` rule is deliberately not copied either — there a business refusal
	# means a gate wearing a propagate's clothes, whereas here a lead that cannot be made is exactly the
	# accident this must absorb. The message's own path is unchanged and still ends in today's drop.
	save_point = f"tc_wa_enrol_{frappe.generate_hash(length=8)}"
	try:
		frappe.db.savepoint(save_point)
		lead = _create(number, event, grain)
	except Exception:
		try:
			frappe.db.rollback(save_point=save_point)
			frappe.log_error(
				title="whatsapp enrolment failed",
				message=f"account={event.account} :: {frappe.get_traceback()}",
			)
		except Exception:  # nosec B110 — log_error is itself an insert and can fail the same way
			pass
		return None
	frappe.db.release_savepoint(save_point)
	return lead


def _create(number, event, grain):
	"""Hand the brain a payload and a grain descriptor, and return the lead's name.

	The descriptor quacks like a `CRM Lead API Mapping` — the brain reads only `.source/.vertical/
	.crm_group/.program` off it, which is the same substitution `intake` and the Desk import make.
	`is_sysmgr=False` with a descriptor present is what forces the grain and drops anything the payload
	might have said about routing; `allowed_programs=[]` is unconsulted because the grain pins it.
	"""
	from tatva_connect.api.partner import _ensure_lead_source, _upsert_one

	# The source master, on first sight. `source` is a ROUTING field, so it rides on the descriptor and
	# never on the payload — putting it in `parent_fields` would collect it past the very guard that stops
	# a caller choosing its own routing. That leaves nobody to create the row, and a Link frappe has never
	# seen refuses on save; this is the helper the partner path already uses for exactly that, asked
	# directly. Not a seed: the row is born from real traffic, on a site that has had some.
	_ensure_lead_source({"source": SOURCE})

	item = {"mobile_no": "+" + number.lstrip("+")}
	parent_fields = ["mobile_no"]
	# The profile name is what the sender chose to publish about themselves, and it is the only name
	# WhatsApp offers. Sent only when it is really there — the brain fills its own placeholder otherwise,
	# and a blank pushed through would overwrite a real name on the update leg.
	if profile_name := _profile_name(event):
		item["first_name"] = profile_name
		parent_fields.append("first_name")

	vertical, group, program = grain
	doc, _action = _upsert_one(
		item,
		frappe._dict(source=SOURCE, vertical=vertical, crm_group=group, program=program),
		False,
		parent_fields,
		{},
		allowed_programs=[],
	)
	return doc.name


def _profile_name(event):
	"""The sender's own display name off the raw payload, or "". Read the way `ingest` already reads it
	so the name on the lead and the name on the message row can never be two different answers."""
	raw = event.raw if isinstance(event.raw, dict) else {}
	return (raw.get("senderName") or "").strip()
