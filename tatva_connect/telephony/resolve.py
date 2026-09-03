"""Resolution — the shared, provider-blind brain between the envelope and the writer.

Two gates, and they do different jobs.

The DID map decides WHETHER a call is kept. An unmapped number is dropped. This is the relevance
gate, and it is what makes a shared provider account safe: a live capture found TatvaCare, Visit,
ICICI and Quest on one Acefone tenant with DIDs interleaved.

The agent identity decides WHO is credited. It never drops a call. A missed call carries no agent at
all (161 of 179 inbound CDRs were never answered) and must still reach the lead.

A dropped call is not a lost call. The spine persists every raw payload before any of this runs, so
anything dropped stays auditable and replayable once its DID is mapped.

The DID map IS the routing table. A number is a child row of the grain that owns it, so a mapped DID
cannot exist without a route, and the two cannot disagree. They were separate tables once, and they
did disagree: a grain carried four DIDs and no routing rule, which left every pull for its leads dead.
"""
import frappe

from tatva_connect import phone
from tatva_connect.telephony import envelope as env

ROUTING_DOCTYPE = "CRM Telephony Routing"
DID_CHILD = "CRM Telephony Routing DID"
# crm's own agent table and the seat column we add to it. ONE owner: the seat answers both "whose phone
# do we ring" (outbound) and "who answered" (inbound), and two readers of one column would drift.
AGENT_DOCTYPE = "CRM Telephony Agent"
SEAT_FIELD = "acefone_number"
SETTINGS = "CRM Telephony Settings"

# The same three axes, spelled two ways: the maps say vertical/psp_group/program, a CRM Lead says
# custom_vertical/custom_group/custom_current_program. The translation is kept in one place.
_GRAIN_AXES = (
	("vertical", "custom_vertical"),
	("psp_group", "custom_group"),
	("program", "custom_current_program"),
)


def is_ours(cdr) -> bool:
	"""Both gates: a call of a wanted kind, on a number that is ours.

	The DID's account must also agree with the account that authenticated the delivery. They can only
	disagree through misconfiguration, and the consequence would be one tenant's token writing another
	tenant's calls — so a mismatch is refused rather than reconciled.
	"""
	grain = grain_for(cdr)
	if not grain or not should_capture(cdr):
		return False

	sender = cdr.get("account")
	owner = grain.get("telephony_account")
	if sender and owner and sender != owner:
		frappe.logger("telephony").warning(
			f"telephony: DID {cdr['did_number']} belongs to {owner}, but the delivery authenticated "
			f"as {sender}; refused"
		)
		return False
	return True


def account_for_did(did_number):
	"""The account a number belongs to, or None. The one DID -> account resolver.

	Read off the DID map, which is where an operator declares it. The live webhook already knows the
	account from the token; this is what the replay and reconcile paths use, and it must agree with
	the token or the two paths would attribute the same call to different accounts.
	"""
	grain = grain_for({"did_number": phone.match_digits(did_number, last=10)})
	return grain.get("telephony_account") if grain else None


def should_capture(cdr) -> bool:
	"""Match a call against the operator's capture rules.

	An empty table captures nothing. Every switch in this app ships dormant; telephony is turned on
	by adding a rule, never by a default. The most specific match wins, and Ignore beats Capture at
	equal specificity so an explicit exception can always be carved out of a broader rule.
	"""
	action, best = None, -1
	for rule in _capture_rules():
		if not rule.get("enabled") or rule.get("provider") != cdr["provider"]:
			continue
		score = _rule_score(rule, cdr)
		if score is None:
			continue
		if score > best or (score == best and rule.get("action") == "Ignore"):
			action, best = rule.get("action"), score
	return action == "Capture"


def _rule_score(rule, cdr):
	"""Specificity of a matching rule, or None when it does not match. 'Any' matches without scoring."""
	score = 0
	for field, value in (("direction", cdr["direction"]), ("channel", cdr["channel"])):
		want = (rule.get(field) or "Any").strip()
		if want == "Any":
			continue
		if want.casefold() != str(value).casefold():
			return None
		score += 1
	return score


def _capture_rules():
	"""The rules off the Settings single. Cached; Frappe invalidates the cache on save."""
	try:
		return [row.as_dict() for row in frappe.get_cached_doc(SETTINGS).get("capture_rules") or []]
	except Exception:
		# Unreadable settings read as no rules, which captures nothing. The safe direction.
		frappe.log_error(title="telephony: capture rules unreadable", message=frappe.get_traceback())
		return []


def grain_for(cdr):
	"""The grain a call belongs to, or None when the number is not ours.

	The number is stored as its last-10 digits and the column is indexed, so this is an indexed lookup
	on each of two tables. It runs inline on every inbound webhook.

	The account is the rule's, unless the number declares its own — which is how one grain reached by
	two providers is expressed, the DID being the only thing that knows which of them carried the call.
	"""
	digits = cdr.get("did_number")
	if not digits or len(digits) < env.PHONE_MIN_DIGITS:
		return None

	did = frappe.db.get_value(
		DID_CHILD,
		{"did_number": digits, "enabled": 1, "parenttype": ROUTING_DOCTYPE},
		["parent", "telephony_account"],
		as_dict=True,
	)
	if not did:
		return None

	grain = frappe.db.get_value(
		ROUTING_DOCTYPE,
		did.parent,
		["vertical", "psp_group", "program", "telephony_account"],
		as_dict=True,
	)
	if not grain:
		return None

	grain["telephony_account"] = did.telephony_account or grain.telephony_account
	return grain


def lead_for(cdr, grain):
	"""The single lead a call belongs to, or None. Never a best guess.

	The rule is `(DID -> grain) + customer phone`. Two facts that are each ambiguous alone intersect
	at one lead: a person with four leads across four programs carries the same phone on all four,
	and the DID says which of the four the call arrived on.

	The DID is not trusted as a fact — it is looked up in an owned table — so the grain comes from
	here, not from the caller. The phone is the only field taken on faith, and it can only select
	within a grain already pinned; it can never widen scope.
	"""
	phone = cdr.get("customer_number")
	if not phone or len(phone) < env.PHONE_MIN_DIGITS or not grain:
		return None

	# Suffix-anchored. A lead may store '+919911232686'; a '%...%' match would also hit a number
	# merely containing these digits.
	filters = {"mobile_no": ["like", f"%{phone}"]}
	for map_field, lead_field in _GRAIN_AXES:
		if grain.get(map_field):
			filters[lead_field] = grain[map_field]

	names = frappe.get_all("CRM Lead", filters=filters, pluck="name", limit=5)
	if len(names) == 1:
		return names[0]
	if len(names) > 1:
		# Two leads sharing a phone inside one grain is a data defect, not a routing choice.
		frappe.logger("telephony").warning(
			f"telephony: {len(names)} leads share {phone} in grain {grain}; call left unlinked"
		)
	return None


def user_for(cdr, grain=None):
	"""The rep who handled a call, or None.

	Two identifiers, asked in the order a provider is likely to send them: the agent's email, which is a
	CRM login on its own, else the SEAT, which the operator already stores to place that rep's calls.
	Neither resolving leaves the rep blank and the call still logged — relevance and attribution are
	separate questions, and a missed call has no agent at all.

	The seat is what carries this account: `answered_agent` sends no email on any of 1,195 answered
	calls, and the seat named the same rep Acefone's own `agent_name` did on all 1,195.
	"""
	email = (cdr.get("agent_key") or "").strip().casefold()
	seat = (cdr.get("agent_extension") or "").strip()
	if not email and not seat:
		return None  # Nobody answered. A missed call has no agent, and that is not an error.

	user = _user_by_email(email) or user_for_seat(seat)
	if not user:
		frappe.logger("telephony").warning(
			f"telephony: agent {email or seat} on account {cdr.get('account')} maps to no CRM user; "
			f"call {cdr.get('call_key')} logged unattributed"
		)
	return user


def _user_by_email(email):
	"""The CRM user whose login this provider email is, or None.

	A provider that names its agents by their corporate address resolves here and needs no config at all.
	One that names them by anything else resolves by SEAT instead — there is no third translation table.
	"""
	if not email:
		return None
	return frappe.db.get_value("User", {"name": email, "enabled": 1}, "name")


def seat_for_user(user):
	"""The provider seat to ring for a rep, or None. The outbound half of the seat table."""
	return frappe.db.get_value(AGENT_DOCTYPE, {"user": user}, SEAT_FIELD)


def user_for_seat(seat):
	"""The rep holding a provider seat, or None. The inbound half, and matched WHOLE: a seat is not a
	phone number, and suffix-matching one against a phone column is the collision this app was already
	burned by."""
	if not seat:
		return None
	return frappe.db.get_value(AGENT_DOCTYPE, {SEAT_FIELD: seat}, "user")
