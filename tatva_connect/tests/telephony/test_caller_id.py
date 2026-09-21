"""The DID a click-to-call shows the patient: the account's Caller ID by default, or another DID of the lead's grain."""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.telephony import bridge
from tatva_connect.telephony.adapters import acefone
from tatva_connect.tests.telephony.fixtures import config

GRAIN_DID = "9000111222"
FOREIGN_DID = "9000333444"
OTHER_ACCOUNT_DID = "9000555666"


class TestCallerId(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		config.ensure_account()
		config.ensure_account(account=config.OTHER_ACCOUNT)

	def setUp(self):
		config.clear_dids()
		rule = config.routing_rule()
		rule.append("dids", {"did_number": GRAIN_DID, "label": "Inside Sales", "enabled": 1})
		rule.append("dids", {"did_number": OTHER_ACCOUNT_DID, "telephony_account": config.OTHER_ACCOUNT, "enabled": 1})
		rule.save(ignore_permissions=True)  # authz-ok: tier-a — test fixture, runs as Administrator
		self.rule = frappe._dict(name=rule.name, telephony_account=rule.telephony_account)
		self.account = frappe.get_doc(acefone.ACCOUNT_DT, config.ACCOUNT)

	def tearDown(self):
		config.clear_dids()

	def test_the_grain_lists_only_its_own_account_dids_with_their_labels(self):
		self.assertEqual(bridge._dids(self.rule), [frappe._dict(did_number=GRAIN_DID, label="Inside Sales")])

	def test_no_pick_sends_the_account_caller_id_as_today(self):
		self.assertEqual(bridge._caller_id(self.rule, self.account, None), self.account.caller_id)

	def test_a_picked_grain_did_is_sent(self):
		self.assertEqual(bridge._caller_id(self.rule, self.account, f"+91{GRAIN_DID}"), GRAIN_DID)

	def test_a_number_outside_the_grain_is_refused(self):
		for number in (FOREIGN_DID, OTHER_ACCOUNT_DID):
			with self.assertRaisesRegex(frappe.ValidationError, "not a number this lead's team calls from"):
				bridge._caller_id(self.rule, self.account, number)
