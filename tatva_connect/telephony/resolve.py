"""Resolution — the shared, provider-blind brain between the envelope and the writer.

Two gates, and they do different jobs.

The DID map decides WHETHER a call is kept. An unmapped number is dropped. This is the relevance
gate, and it is what makes a shared provider account safe: a live capture found TatvaCare, Visit,
ICICI and Quest on one Acefone tenant with DIDs interleaved.

The agent map decides WHO is credited. It never drops a call. A missed call carries no agent at all
(161 of 179 inbound CDRs were never answered) and must still reach the lead.

A dropped call is not a lost call. The spine persists every raw payload before any of this runs, so
anything dropped stays auditable and replayable once its DID is mapped.

The DID map IS the routing table. A number is a child row of the grain that owns it, so a mapped DID
cannot exist without a route, and the two cannot disagree. They were separate tables once, and they
did disagree: a grain carried four DIDs and no routing rule, which left every pull for its leads dead.
"""
import frappe

from tatva_connect.telephony import envelope as env

ROUTING_DOCTYPE = "CRM Telephony Routing"
DID_CHILD = "CRM Telephony Routing DID"
AGENT_MAP_DOCTYPE = "CRM Telephony Agent Map"
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
	grain = grain_for({"did_number": env.phone_digits(did_number)})
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

	Resolved in three steps: the provider's agent email is a CRM user; else an operator declared the
	translation in the agent map; else the rep is left blank and the call is still logged.

	The map is not optional in practice. Of 24 agents in a live capture, one used a corporate
	address, twenty a partner company's domain, and three personal Gmail accounts — nothing can
	infer a CRM user from the last of those.
	"""
	email = (cdr.get("agent_key") or "").strip().casefold()
	if not email:
		return None  # Nobody answered. A missed call has no agent, and that is not an error.

	user = frappe.db.get_value("User", {"name": email, "enabled": 1}, "name")
	if user:
		return user

	mapping = frappe.db.get_value(
		AGENT_MAP_DOCTYPE,
		{"agent_email": email, "telephony_account": cdr.get("account"), "enabled": 1},
		["user", "vertical", "psp_group", "program"],
		as_dict=True,
	)
	if not mapping:
		frappe.logger("telephony").warning(
			f"telephony: agent {email} on account {cdr.get('account')} maps to no CRM user; "
			f"call {cdr.get('call_key')} logged unattributed"
		)
		return None

	if grain and _grain_mismatch(mapping, grain):
		# Not fatal — the call belongs to the lead either way — but an agent working outside their
		# grain is worth surfacing on a shared account.
		frappe.logger("telephony").warning(
			f"telephony: agent {email} answered on grain {grain} but is mapped elsewhere; attributed anyway"
		)
	return mapping["user"]


def _grain_mismatch(mapping, grain) -> bool:
	"""True when an agent declares an axis that the call's grain contradicts. Declaring none matches all."""
	return any(
		mapping.get(axis) and grain.get(axis) and mapping[axis] != grain[axis]
		for axis, _ in _GRAIN_AXES
	)
