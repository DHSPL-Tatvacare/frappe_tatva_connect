# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The operator configuration a telephony test needs — the ONE place a suite says how it is shaped.

Telephony ships INERT. A call is captured only when three operator rows agree: an enabled account, a DID
mapped to a grain on a routing rule, and a capture rule that admits the call's direction and channel.
`test_resolve_gates` had all of this as module-private helpers, so the next suite either duplicated it or
— what actually happened — was written without it and asserted against a call that was silently declined.

`test_reconcile_never_deletes.test_a_call_is_found_by_its_key_column_not_by_the_row_name` died on its own
setup line for months for exactly that reason: `row_for_key` returned None because the record was never
captured, so the rule the test is NAMED for was never reached and was, in fact, unguarded.

Same shape as `workflow_engine/tests/fixtures.py`: a suite says what configuration it wants, not how the
rows are stored.

RESTORE WHAT YOU BORROW. The capture rules live on a Single, so a suite that sets them overwrites
whatever the bench already had — `current_rules()` exists so they can be handed back exactly.
"""
import json
import os

import frappe

from tatva_connect.automation.registry import parent_of
from tatva_connect.telephony import resolve

ACCOUNT = "_TestTelephonyAcct"
OTHER_ACCOUNT = "_TestTelephonyAcctOther"
# Another business line on ACCOUNT's own provider tenant: same token, its own account.
SIBLING_ACCOUNT = "_TestTelephonyAcctSibling"
GRAIN = {"vertical": "_TestTelVertical", "psp_group": None, "program": None}

_PROVIDER = {"provider": "Acefone", "api_token": "resolve-gates-test-token", "caller_id": "919000100001"}


def rule(direction, channel, action="Capture"):
	"""One capture rule, as an operator would add it."""
	return {**_PROVIDER, "direction": direction, "channel": channel, "action": action, "enabled": 1}


def current_rules():
	"""The site's capture rules as plain dicts, so they can be put back exactly as they were."""
	settings = frappe.get_single("CRM Telephony Settings")
	return [
		{"provider": r.provider, "direction": r.direction, "channel": r.channel,
		 "action": r.action, "enabled": r.enabled}
		for r in (settings.capture_rules or [])
	]


def set_rules(rules):
	settings = frappe.get_single("CRM Telephony Settings")
	settings.set("capture_rules", [])
	for one in rules:
		settings.append("capture_rules", one)
	settings.save(ignore_permissions=True)  # authz-ok: tier-a — test fixture, runs as Administrator
	frappe.db.commit()
	frappe.clear_cache(doctype="CRM Telephony Settings")


def routing_rule():
	"""The routing rule for the test grain, created once. The DID map is a child table on it."""
	name = frappe.db.get_value("CRM Telephony Routing", {"vertical": GRAIN["vertical"]}, "name")
	if name:
		return frappe.get_doc("CRM Telephony Routing", name)
	doc = frappe.new_doc("CRM Telephony Routing")
	doc.update({"telephony_account": ACCOUNT, **GRAIN})
	doc.insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture, runs as Administrator
	return doc


def map_did(digits, enabled=1, valid_to=None):
	"""Map a number onto the test grain. A DID is a row on the rule that owns it, so mapping one cannot
	leave it without a route."""
	rule_doc = routing_rule()
	rule_doc.append("dids", {"did_number": f"+91{digits}", "enabled": enabled, "valid_to": valid_to})
	rule_doc.save(ignore_permissions=True)  # authz-ok: tier-a — test fixture, runs as Administrator
	frappe.db.commit()


def clear_dids():
	"""Drop the test routing rule, and its numbers with it; its calls go first, since a number that logged one cannot be removed."""
	# A sibling account on the same tenant delivers onto this rule's numbers too, so its calls go as well.
	for name in frappe.get_all("CRM Call Log", filters={"custom_telephony_account": ["in", (ACCOUNT, SIBLING_ACCOUNT)]}, pluck="name"):
		frappe.delete_doc("CRM Call Log", name, force=True, ignore_permissions=True)
	for name in frappe.get_all("CRM Telephony Routing", filters={"telephony_account": ACCOUNT}, pluck="name"):
		frappe.delete_doc("CRM Telephony Routing", name, force=True, ignore_permissions=True)
	frappe.db.commit()


def set_seat(user, seat, account=ACCOUNT, follow_me_number=None):
	"""Give a rep an extension on one account, as an operator would on their Telephony Agent row."""
	doc = _agent(user)
	doc.set(resolve.SEAT_FIELD, [r for r in doc.get(resolve.SEAT_FIELD) if r.telephony_account != account])
	doc.append(resolve.SEAT_FIELD, {"telephony_account": account, "extension": seat, "follow_me_number": follow_me_number})
	doc.save(ignore_permissions=True)  # authz-ok: tier-a — test fixture, runs as Administrator
	frappe.db.commit()


def hand_over(seat, giver, taker, at, account=ACCOUNT):
	"""Close the giver's holding of a seat at `at` and open the taker's from it, as an operator hands a leaver's extension on."""
	doc = _agent(giver)
	for row in doc.get(resolve.SEAT_FIELD):
		if row.telephony_account == account and row.extension == seat:
			row.valid_to = at
	doc.save(ignore_permissions=True)  # authz-ok: tier-a — test fixture, runs as Administrator
	doc = _agent(taker)
	doc.append(resolve.SEAT_FIELD, {"telephony_account": account, "extension": seat, "valid_from": at})
	doc.save(ignore_permissions=True)  # authz-ok: tier-a — test fixture, runs as Administrator
	frappe.db.commit()


def clear_seats(account=ACCOUNT):
	"""Drop every extension on a test account, from whichever rep holds it."""
	for name in frappe.get_all(resolve.SEAT_CHILD, filters={"parenttype": resolve.AGENT_DOCTYPE, "telephony_account": account}, pluck="parent"):
		doc = frappe.get_doc(resolve.AGENT_DOCTYPE, name)
		doc.set(resolve.SEAT_FIELD, [r for r in doc.get(resolve.SEAT_FIELD) if r.telephony_account != account])
		doc.save(ignore_permissions=True)  # authz-ok: tier-a — test fixture, runs as Administrator
	frappe.db.commit()


def _agent(user):
	"""The rep's crm agent row, created as an operator would when it is missing."""
	if frappe.db.exists(resolve.AGENT_DOCTYPE, user):
		return frappe.get_doc(resolve.AGENT_DOCTYPE, user)
	return frappe.get_doc({"doctype": resolve.AGENT_DOCTYPE, "user": user}).insert(
		ignore_permissions=True  # authz-ok: tier-a — test fixture, runs as Administrator
	)


def ensure_account(rep_emails=(), account=ACCOUNT, token=None):
	"""The minimum an operator would configure: a grain, an enabled account with its own token (or the given one), and the reps who answer."""
	token = token or f"{account}-token"
	if not frappe.db.exists("CRM Vertical", GRAIN["vertical"]):
		frappe.get_doc({"doctype": "CRM Vertical", "vertical_name": GRAIN["vertical"]}).insert(
			ignore_permissions=True  # authz-ok: tier-a — test fixture, runs as Administrator
		)
	if not frappe.db.exists("CRM Telephony Account", account):
		frappe.get_doc({
			"doctype": "CRM Telephony Account", "account_name": account, **_PROVIDER, "api_token": token, "enabled": 1,
		}).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture, runs as Administrator
	doc = frappe.get_doc("CRM Telephony Account", account)
	if doc.get_password("api_token", raise_exception=False) != token:
		doc.api_token = token
		doc.save(ignore_permissions=True)  # authz-ok: tier-a — test fixture, runs as Administrator
	for email in rep_emails:
		if not frappe.db.exists("User", email):
			frappe.get_doc({
				"doctype": "User", "email": email, "first_name": "Telephony Rep", "send_welcome_email": 0,
			}).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture, runs as Administrator
	frappe.db.commit()


# -- Captured provider shapes and the records a call lands on ----------------------------------------------------------

_HERE = os.path.dirname(__file__)


def corpus():
	"""179 anonymised Acefone hangup webhooks from a live shared tenant (2026-07)."""
	with open(os.path.join(_HERE, "acefone_cdr_corpus.jsonl")) as fh:
		return [json.loads(line) for line in fh if line.strip()]


def record(case):
	"""One anonymised Acefone call record of 2026-10-15, by its `_case`."""
	with open(os.path.join(_HERE, "acefone_records_2026-10-15.jsonl")) as fh:
		return next(row for row in map(json.loads, fh) if row["_case"] == case)


def lead(digits, owner=None, vertical=None):
	"""A lead with this number on a grain, through the document API."""
	vertical = vertical or GRAIN["vertical"]
	if not frappe.db.exists("CRM Vertical", vertical):
		frappe.get_doc({"doctype": "CRM Vertical", "vertical_name": vertical}).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture
	doc = frappe.get_doc({
		"doctype": "CRM Lead", "first_name": "ZZ telephony", "mobile_no": f"+91{digits}",
		"custom_vertical": vertical, "lead_owner": owner,
	}).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture, runs as Administrator
	frappe.db.commit()
	return doc


def credit_related_only(on):
	"""The operator's switch on CRM Telephony Settings."""
	settings = frappe.get_single(resolve.SETTINGS)
	settings.set(resolve.RELATED_ONLY_FIELD, int(on))
	settings.save(ignore_permissions=True)  # authz-ok: tier-a — test fixture, runs as Administrator
	frappe.db.commit()


def switch_on(key):
	"""Turn an automation switch and its ancestors on; returns the undo."""
	saved = {}
	while key:
		saved[key] = frappe.db.get_value("CRM Tatva Automation", key, "enabled")
		frappe.db.set_value("CRM Tatva Automation", key, "enabled", 1)
		key = parent_of(key)
	frappe.db.commit()

	def undo():
		for name, enabled in saved.items():
			frappe.db.set_value("CRM Tatva Automation", name, "enabled", enabled)
		frappe.db.commit()
	return undo
