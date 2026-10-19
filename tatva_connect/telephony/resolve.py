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
from frappe import _
from frappe.utils import get_datetime

from tatva_connect import phone
from tatva_connect.telephony import cache
from tatva_connect.telephony import envelope as env

ROUTING_DOCTYPE = "CRM Telephony Routing"
DID_CHILD = "CRM Telephony Routing DID"
# A seat is asked for on exactly one account: the lead's routed account to call, the number's owning account to credit.
ACCOUNT_DOCTYPE = "CRM Telephony Account"
AGENT_DOCTYPE = "CRM Telephony Agent"
SEAT_CHILD = "CRM Telephony Agent Extension"
SEAT_FIELD = "telephony_extensions"
SETTINGS = "CRM Telephony Settings"
RELATED_ONLY_FIELD = "credit_related_reps_only"

# The same three axes, spelled two ways: the maps say vertical/psp_group/program, a CRM Lead says
# custom_vertical/custom_group/custom_current_program. The translation is kept in one place.
_GRAIN_AXES = (
	("vertical", "custom_vertical"),
	("psp_group", "custom_group"),
	("program", "custom_current_program"),
)


def is_ours(cdr) -> bool:
	"""Both gates: a call of a wanted kind, on a number that is ours.

	The DID's account must also be the same tenant as the account that authenticated the delivery. They can
	only differ through misconfiguration, and the consequence would be one tenant's token writing another
	tenant's calls — so a mismatch is refused rather than reconciled.
	"""
	grain = grain_for(cdr)
	if not grain or not should_capture(cdr):
		return False

	sender = cdr.get("account")
	owner = grain.get("telephony_account")
	# Accounts sharing one token are one tenant split by business line, so a delivery on a sibling's number is that tenant's own.
	if sender and owner and owner != sender and owner not in tenant_of(sender):
		frappe.logger("telephony").warning(
			f"telephony: DID {cdr['did_number']} belongs to {owner}, another tenant than the delivery's "
			f"{sender}; refused"
		)
		return False
	return True


def account_for_did(did_number, at=None):
	"""The account a number belongs to, or None. The one DID -> account resolver.

	Read off the DID map, which is where an operator declares it. The live webhook already knows the
	account from the token; this is what the replay and reconcile paths use, and it must agree with
	the token or the two paths would attribute the same call to different accounts.
	"""
	grain = grain_for({"did_number": phone.match_digits(did_number, last=10), "started_at": at})
	return grain.get("telephony_account") if grain else None


def grain_of_call(call):
	"""(vertical, group, program) of the number a Call Log rang through, lead or no lead; None when the number is not ours."""
	# `medium` holds the DID on both directions (writer.py), and the call's own start picks the dated row.
	grain = grain_for({"did_number": phone.match_digits(call.get("medium"), last=10), "started_at": call.get("start_time")})
	return tuple(grain.get(axis) or "" for axis, _lead_field in _GRAIN_AXES) if grain else None


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

	at = moment(cdr.get("started_at"))
	did = next((r for r in frappe.get_all(
		DID_CHILD,
		filters={"did_number": digits, "enabled": 1, "parenttype": ROUTING_DOCTYPE},
		fields=["parent", "telephony_account", "valid_from", "valid_to"],
	) if held_at(r, at)), None)
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
	# The stored form, matched exactly: a lead's number is canonical on save, so this seeks the (mobile_no, vertical, group) index.
	mobile = cdr.get("customer_e164")
	if not mobile or not grain:
		return None
	filters = {"mobile_no": mobile}
	for map_field, lead_field in _GRAIN_AXES:
		if grain.get(map_field):
			filters[lead_field] = grain[map_field]

	names = frappe.get_all("CRM Lead", filters=filters, pluck="name", limit=5)
	if len(names) == 1:
		return names[0]
	if len(names) > 1:
		# Two leads sharing a phone inside one grain is a data defect, not a routing choice.
		frappe.logger("telephony").warning(
			f"telephony: {len(names)} leads share {mobile} in grain {grain}; call left unlinked"
		)
	return None


def user_for(cdr, grain):
	"""The rep who handled a call, or None.

	Three identifiers: the SEAT, held over dates, so the rep is whoever held it when the call started; else
	the agent's email, which is a CRM login on its own, for a seat nobody holds; else the follow-me phone on that seat's row.
	None resolving leaves the rep blank and the call still logged — relevance and attribution are
	separate questions, and a missed call has no agent at all.

	The seat is what carries this account: `answered_agent` sends no email on any of 1,195 answered
	calls, and the seat named the same rep Acefone's own `agent_name` did on all 1,195.

	Seats are read on the account that owns the number (`grain`), never on whichever sibling's token delivered it.
	"""
	email = (cdr.get("agent_key") or "").strip().casefold()
	seat = (cdr.get("agent_extension") or "").strip()
	agent_phone = (cdr.get("agent_phone") or "").strip()
	if not email and not seat and not agent_phone:
		return None  # Nobody answered. A missed call has no agent, and that is not an error.

	account = grain["telephony_account"]
	# Follow-me last: it only names a rep where the seat and the email named nobody.
	user = (
		user_for_seat(seat, account, cdr.get("started_at"))
		or _user_by_email(email)
		or user_for_phone(agent_phone, account, cdr.get("started_at"))
	)
	if not user:
		frappe.logger("telephony").warning(
			f"telephony: agent {seat or email or 'follow-me number'} on account {account} maps to no CRM user; "
			f"call {cdr.get('call_key')} logged unattributed"
		)
	return user


def may_credit(user, lead) -> bool:
	"""Whether an outgoing call on this lead may be credited to this rep: always, unless the switch is on and they neither own nor hold it."""
	if not user or not lead:
		return True
	try:
		if not frappe.get_cached_doc(SETTINGS).get(RELATED_ONLY_FIELD):
			return True
		owner, assigned = frappe.db.get_value("CRM Lead", lead, ["lead_owner", "_assign"]) or (None, None)
		return user == owner or user in (frappe.parse_json(assigned) or [])
	except Exception:
		# An unreadable setting or lead credits the call as it always was; the check never costs a call.
		frappe.log_error(title="telephony: lead relation unreadable", message=frappe.get_traceback())
		return True


def _user_by_email(email):
	"""The CRM user whose login this provider email is, or None.

	A provider that names its agents by their corporate address resolves here and needs no config at all.
	One that names them by anything else resolves by SEAT instead — there is no third translation table.
	"""
	if not email:
		return None
	return frappe.db.get_value("User", {"name": email, "enabled": 1}, "name")


def seats_for_account(account):
	"""Every holding of an extension on one account, with the dates it was held."""
	return cache.read("seat_rows", account, lambda: frappe.get_all(
		SEAT_CHILD,
		filters={"parenttype": AGENT_DOCTYPE, "telephony_account": account},
		fields=["parent", "extension", "follow_me_number", "valid_from", "valid_to"],
	))


def holdings_on_tenant(account, at=None):
	"""Who holds each extension on this account's whole tenant at a moment, as {extension: rep}: one seat is one person tenant-wide."""
	return {seat: rep for name in tenant_of(account) for seat, rep in holdings(name, at).items()}


def tenant_of(account):
	"""The accounts that are one provider tenant with this one (same provider and API token), itself included."""
	if not account:
		return []
	return cache.read("tenant", account, lambda: _same_tenant(account))


def _same_tenant(account):
	"""Read uncached: only names leave this function, never a token."""
	key = _tenant_key(account)
	if not key:
		return [account]
	return [name for name in frappe.get_all(ACCOUNT_DOCTYPE, pluck="name") if name == account or _tenant_key(name) == key]


def _tenant_key(account):
	"""(provider, API token), or None for an account with no token, which shares nothing."""
	doc = frappe.get_doc(ACCOUNT_DOCTYPE, account)
	token = doc.get_password("api_token", raise_exception=False)
	return (doc.provider, token) if token else None


def moment(at=None):
	"""A moment as the rows store it, now by default; an epoch stamp parses aware, in site time, and the rows are naive."""
	return get_datetime(at).replace(tzinfo=None)


def held_at(row, at):
	"""Whether a dated holding (an extension or a DID row) covers this moment; a blank bound is open."""
	return (not row.valid_from or row.valid_from <= at) and (not row.valid_to or at < row.valid_to)


def overlaps(a, b):
	"""Whether two dated holdings share any moment; a blank bound is open."""
	return (not a.valid_to or not b.valid_from or b.valid_from < a.valid_to) and (
		not b.valid_to or not a.valid_from or a.valid_from < b.valid_to
	)


def assert_dated(rows):
	"""Read each row's Valid From / Valid To as datetimes, and refuse one that ends before it starts."""
	for row in rows:
		row.valid_from = get_datetime(row.valid_from) if row.valid_from else None
		row.valid_to = get_datetime(row.valid_to) if row.valid_to else None
		if row.valid_from and row.valid_to and row.valid_to <= row.valid_from:
			frappe.throw(_("Row {0}: Valid To must be after Valid From.").format(row.idx), title=_("Invalid dates"))


def holdings(account, at=None):
	"""Who held each extension on this account at a moment (now by default), as {extension: rep}."""
	at = moment(at)
	return {r.extension: r.parent for r in seats_for_account(account) if held_at(r, at)}


def seat_for_user(user, account):
	"""The rep's extension on this account now, or None."""
	if not user or not account:
		return None
	return next((seat for seat, rep in holdings(account).items() if rep == user), None)


def user_for_seat(seat, account, at=None):
	"""The rep holding this extension on this account at a moment, or None; matched whole, never as a phone number."""
	if not seat or not account:
		return None
	return holdings(account, at).get(seat)


def user_for_phone(number, account, at=None):
	"""The one rep whose follow-me phone this is on this account at a moment, or None; a phone two reps hold names nobody."""
	digits = phone.match_digits(number, last=10)
	if not digits or not account:
		return None
	at = moment(at)
	reps = {
		r.parent for r in seats_for_account(account)
		if held_at(r, at) and phone.match_digits(r.get("follow_me_number"), last=10) == digits
	}
	return reps.pop() if len(reps) == 1 else None
