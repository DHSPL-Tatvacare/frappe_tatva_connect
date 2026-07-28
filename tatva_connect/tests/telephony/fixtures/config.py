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
import frappe

ACCOUNT = "_TestTelephonyAcct"
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


def map_did(digits, enabled=1):
	"""Map a number onto the test grain. A DID is a row on the rule that owns it, so mapping one cannot
	leave it without a route."""
	rule_doc = routing_rule()
	rule_doc.append("dids", {"did_number": f"+91{digits}", "enabled": enabled})
	rule_doc.save(ignore_permissions=True)  # authz-ok: tier-a — test fixture, runs as Administrator
	frappe.db.commit()


def clear_dids():
	"""Drop the test routing rule, and its numbers with it."""
	for name in frappe.get_all("CRM Telephony Routing", filters={"telephony_account": ACCOUNT}, pluck="name"):
		frappe.delete_doc("CRM Telephony Routing", name, force=True, ignore_permissions=True)
	frappe.db.commit()


def ensure_account(rep_emails=()):
	"""The minimum an operator would configure: a grain, an enabled account, and the reps who answer."""
	if not frappe.db.exists("CRM Vertical", GRAIN["vertical"]):
		frappe.get_doc({"doctype": "CRM Vertical", "vertical_name": GRAIN["vertical"]}).insert(
			ignore_permissions=True  # authz-ok: tier-a — test fixture, runs as Administrator
		)
	if not frappe.db.exists("CRM Telephony Account", ACCOUNT):
		frappe.get_doc({
			"doctype": "CRM Telephony Account", "account_name": ACCOUNT, **_PROVIDER, "enabled": 1,
		}).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture, runs as Administrator
	for email in rep_emails:
		if not frappe.db.exists("User", email):
			frappe.get_doc({
				"doctype": "User", "email": email, "first_name": "Telephony Rep", "send_welcome_email": 0,
			}).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture, runs as Administrator
	frappe.db.commit()
