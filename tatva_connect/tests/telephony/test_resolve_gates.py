"""The two gates, tested against the real capture.

The DID map decides whether a call is kept. The agent map decides who is credited, and never drops a
call. Both are exercised here against `fixtures/acefone_cdr_corpus.jsonl` — 179 anonymised CDRs from a
live shared Acefone tenant carrying four businesses' traffic.
"""
import json
import os

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.telephony.adapters import acefone

CORPUS = os.path.join(os.path.dirname(__file__), "fixtures", "acefone_cdr_corpus.jsonl")

ACCOUNT = "_TestTelephonyAcct"
BUSY_DID = "9000007179"      # 37 Dialer calls, 8 of them answered
IVR_DID = "9000000210"       # 18 IVR calls, none ever answered
GRAIN = {"vertical": "_TestTelVertical", "psp_group": None, "program": None}

# The agent the anonymised corpus carries on its answered CDRs. The auto-match is proven by making
# this a CRM user; the map is proven by an address that deliberately is not one.
REP = "rep@example.com"
FOREIGN = "_test_tel_foreign@example.com"


def _corpus():
	with open(CORPUS) as fh:
		return [json.loads(line) for line in fh if line.strip()]


class TestTelephonyGates(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.payloads = _corpus()
		# The capture rules live on a Single, so a test that sets them overwrites whatever the site
		# already had. They are stashed and given back, or running this suite against a configured bench
		# would silently destroy the operator's telephony configuration.
		cls.saved_rules = _current_rules()
		_ensure_fixtures()

	@classmethod
	def tearDownClass(cls):
		_set_rules(cls.saved_rules)
		super().tearDownClass()

	def setUp(self):
		_set_rules([])
		_clear_dids()
		_clear("CRM Telephony Agent Map")
		_clear_calls()

	def tearDown(self):
		_set_rules([])
		_clear_calls()

	def _run_corpus(self):
		kept = [
			name
			for payload in self.payloads
			if (name := acefone.process(payload, event="inbound_complete", account=ACCOUNT))
		]
		return len(kept)

	def test_no_capture_rules_captures_nothing(self):
		"""The dormant default. An empty rule table must never be read as 'capture everything'."""
		_map_did(BUSY_DID)
		self.assertEqual(self._run_corpus(), 0)

	def test_capture_rule_without_a_mapped_did_captures_nothing(self):
		"""The relevance gate stands alone: wanting a kind of call is not the same as owning the number."""
		_set_rules([_rule("Inbound", "Dialer")])
		self.assertEqual(self._run_corpus(), 0)

	def test_only_calls_on_a_mapped_did_are_kept(self):
		"""One DID mapped out of the corpus's many. Three other businesses' traffic must be dropped."""
		_set_rules([_rule("Inbound", "Dialer")])
		_map_did(BUSY_DID)
		kept = self._run_corpus()

		self.assertEqual(kept, 37)
		self.assertLess(kept, len(self.payloads))
		landed = frappe.get_all("CRM Call Log", filters={"custom_telephony_account": ACCOUNT}, pluck="to")
		self.assertEqual(set(landed), {BUSY_DID})

	def test_ignore_rule_beats_a_broader_capture(self):
		"""An explicit exception must be able to be carved out of a wider rule."""
		_map_did(BUSY_DID)
		_set_rules([_rule("Inbound", "Any"), _rule("Inbound", "IVR", action="Ignore")])
		self.assertEqual(self._run_corpus(), 37)

	def test_missed_calls_reach_the_lead(self):
		"""A missed call carries no agent. It must still be logged: nobody handled it, and that is the point."""
		_set_rules([_rule("Inbound", "IVR")])
		_map_did(IVR_DID)
		self.assertEqual(self._run_corpus(), 18)

		rows = frappe.get_all(
			"CRM Call Log", filters={"custom_telephony_account": ACCOUNT}, fields=["status", "receiver"]
		)
		self.assertEqual({r.status for r in rows}, {"No Answer"})
		self.assertEqual([r.receiver for r in rows if r.receiver], [])

	def test_agent_resolves_when_the_provider_email_is_a_crm_user(self):
		"""The auto-match. Staff whose provider email is their login need no configuration at all."""
		_set_rules([_rule("Inbound", "Dialer")])
		_map_did(BUSY_DID)
		self._run_corpus()

		answered = frappe.get_all(
			"CRM Call Log",
			filters={"custom_telephony_account": ACCOUNT, "status": "Completed"},
			pluck="receiver",
		)
		self.assertEqual(len(answered), 8)
		self.assertEqual(set(answered), {REP})

	def test_an_unmapped_agent_never_costs_the_call(self):
		"""A foreign agent on an owned DID: the call is kept, the rep is left blank."""
		_set_rules([_rule("Inbound", "Dialer")])
		_map_did(BUSY_DID)

		name = acefone.process(_foreign_payload(), event="inbound_complete", account=ACCOUNT)
		self.assertTrue(name)
		self.assertIsNone(frappe.db.get_value("CRM Call Log", name, "receiver"))

	def test_the_agent_map_resolves_what_the_auto_match_cannot(self):
		"""The declared translation. Nothing can infer a CRM user from a personal address."""
		_set_rules([_rule("Inbound", "Dialer")])
		_map_did(BUSY_DID)
		_map_agent(FOREIGN, REP)

		name = acefone.process(_foreign_payload(), event="inbound_complete", account=ACCOUNT)
		self.assertEqual(frappe.db.get_value("CRM Call Log", name, "receiver"), REP)

	def test_replay_and_reconcile_resolve_the_same_account_as_the_live_delivery(self):
		"""The account is read off the DID map, which is where an operator declares it.

		It used to be re-derived from the account's `caller_id` — one number per account, but many DIDs
		per account — so every replayed and every reconciled call was written under no account at all.
		That silently killed agent attribution and broke recording playback, which needs the account to
		find its API token.
		"""
		_set_rules([_rule("Inbound", "Dialer")])
		_map_did(BUSY_DID)
		payload = _foreign_payload()

		# No account passed, exactly as replay and reconcile call it.
		self.assertEqual(acefone.account_for_payload(payload, "inbound_complete"), ACCOUNT)

		name = acefone.process(payload, event="inbound_complete", account=None)
		self.assertEqual(
			frappe.db.get_value("CRM Call Log", name, "custom_telephony_account"),
			ACCOUNT,
			"a replayed call must carry the same account a live one does",
		)

	def test_a_did_belonging_to_another_account_is_refused(self):
		"""The DID's owner must agree with the account that authenticated the delivery.

		They can only disagree through misconfiguration, and the consequence would be one tenant's token
		writing another tenant's calls.
		"""
		_set_rules([_rule("Inbound", "Dialer")])
		_map_did(BUSY_DID)

		payload = _foreign_payload()
		wanted, reason = acefone.screen(payload, "inbound_complete", "_TestSomeOtherAccount")
		self.assertFalse(wanted)
		self.assertIn("different telephony account", reason)

	def test_a_disabled_did_reads_as_unmapped(self):
		"""Disabling a DID must drop its calls, not merely hide the row."""
		_set_rules([_rule("Inbound", "Dialer")])
		_map_did(BUSY_DID, enabled=0)
		self.assertEqual(self._run_corpus(), 0)


def _rule(direction, channel, action="Capture"):
	return {
		"provider": "Acefone",
		"direction": direction,
		"channel": channel,
		"action": action,
		"enabled": 1,
	}


def _current_rules():
	"""The site's capture rules as plain dicts, so they can be put back exactly as they were."""
	settings = frappe.get_single("CRM Telephony Settings")
	return [
		{"provider": r.provider, "direction": r.direction, "channel": r.channel,
		 "action": r.action, "enabled": r.enabled}
		for r in (settings.capture_rules or [])
	]


def _set_rules(rules):
	settings = frappe.get_single("CRM Telephony Settings")
	settings.set("capture_rules", [])
	for rule in rules:
		settings.append("capture_rules", rule)
	settings.save(ignore_permissions=True)
	frappe.db.commit()
	frappe.clear_cache(doctype="CRM Telephony Settings")


def _rule_doc():
	"""The routing rule for the suite's grain, created once. The DID map is a child table on it."""
	name = frappe.db.get_value("CRM Telephony Routing", {"vertical": GRAIN["vertical"]}, "name")
	if name:
		return frappe.get_doc("CRM Telephony Routing", name)
	doc = frappe.new_doc("CRM Telephony Routing")
	doc.update({"telephony_account": ACCOUNT, **GRAIN})
	doc.insert(ignore_permissions=True)
	return doc


def _map_did(digits, enabled=1):
	"""Map a number onto the suite's grain. A DID is a row on the rule that owns it, so mapping one
	cannot leave it without a route."""
	rule = _rule_doc()
	rule.append("dids", {"did_number": f"+91{digits}", "enabled": enabled})
	rule.save(ignore_permissions=True)
	frappe.db.commit()


def _clear_dids():
	"""Drop the suite's routing rule, and its numbers with it."""
	for name in frappe.get_all(
		"CRM Telephony Routing", filters={"telephony_account": ACCOUNT}, pluck="name"
	):
		frappe.delete_doc("CRM Telephony Routing", name, force=True, ignore_permissions=True)
	frappe.db.commit()


def _map_agent(agent_email, user):
	doc = frappe.new_doc("CRM Telephony Agent Map")
	doc.update({"agent_email": agent_email, "user": user, "telephony_account": ACCOUNT, "enabled": 1})
	doc.insert(ignore_permissions=True)
	frappe.db.commit()


def _foreign_payload():
	"""An answered CDR on an owned DID, handled by an agent who is not a CRM user."""
	payload = next(
		dict(p)
		for p in _corpus()
		if str(p.get("call_connected")) == "1" and BUSY_DID in str(p.get("call_to_number"))
	)
	payload["call_id"] = "_test-foreign-agent"
	payload["answered_agent"] = [{"name": "Redacted", "email": FOREIGN, "number": "Extension-0"}]
	return payload


def _clear(doctype):
	"""Drop only the rows this suite owns. An unfiltered delete here would take the operator's DID and
	agent maps with it, and the whole point of the DID map is that losing it drops live calls."""
	for name in frappe.get_all(doctype, filters={"telephony_account": ACCOUNT}, pluck="name"):
		frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
	frappe.db.commit()


def _clear_calls():
	for name in frappe.get_all("CRM Call Log", filters={"custom_telephony_account": ACCOUNT}, pluck="name"):
		frappe.delete_doc("CRM Call Log", name, force=True, ignore_permissions=True)
	frappe.db.commit()


def _ensure_fixtures():
	"""The minimum an operator would configure: a grain, an account, and the rep who answers."""
	if not frappe.db.exists("CRM Vertical", GRAIN["vertical"]):
		frappe.get_doc({"doctype": "CRM Vertical", "vertical_name": GRAIN["vertical"]}).insert(
			ignore_permissions=True
		)
	if not frappe.db.exists("CRM Telephony Account", ACCOUNT):
		frappe.get_doc(
			{
				"doctype": "CRM Telephony Account",
				"account_name": ACCOUNT,
				"provider": "Acefone",
				"enabled": 1,
			}
		).insert(ignore_permissions=True)
	for email in (REP,):
		if not frappe.db.exists("User", email):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": "Telephony Rep",
					"send_welcome_email": 0,
				}
			).insert(ignore_permissions=True)
	frappe.db.commit()
