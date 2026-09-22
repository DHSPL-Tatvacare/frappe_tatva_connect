"""Outbound routes by the RECORD on screen: its grain picks the account and the numbers it may call from."""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.telephony import bridge
from tatva_connect.telephony.adapters import acefone
from tatva_connect.tests.telephony.fixtures import config

GRAIN_DID = "9000111222"
FOREIGN_DID = "9000333444"
OTHER_ACCOUNT_DID = "9000555666"
SHARED_PHONE = "+919000777888"
OTHER_VERTICAL = "_TestTelVerticalTwo"
UNROUTED_VERTICAL = "_TestTelVerticalUnrouted"


class TestOutboundRouting(FrappeTestCase):
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
		# The second grain's rule is this suite's own; leaving it behind would give the next run a route it never made.
		for name in frappe.get_all("CRM Telephony Routing", filters={"vertical": ["in", [OTHER_VERTICAL, UNROUTED_VERTICAL]]}, pluck="name"):
			frappe.delete_doc("CRM Telephony Routing", name, force=True, ignore_permissions=True)
		for name in frappe.get_all("CRM Lead", filters={"mobile_no": SHARED_PHONE}, pluck="name"):
			frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_the_grain_offers_its_own_account_dids_with_their_labels(self):
		self.assertEqual(
			bridge._caller_pool(self.rule, self.account),
			[frappe._dict(did_number=GRAIN_DID, label="Inside Sales")],
		)

	def test_a_grain_with_no_dids_calls_from_its_account_caller_id(self):
		config.clear_dids()
		rule = config.routing_rule()
		pool = bridge._caller_pool(frappe._dict(name=rule.name, telephony_account=config.ACCOUNT), self.account)
		self.assertEqual([n.did_number for n in pool], [self.account.caller_id[-10:]])

	def test_a_picked_grain_did_is_sent(self):
		self.assertEqual(bridge._caller_id(self.rule, self.account, f"+91{GRAIN_DID}"), GRAIN_DID)

	def test_a_number_outside_the_grain_is_refused(self):
		for number in (FOREIGN_DID, OTHER_ACCOUNT_DID):
			with self.assertRaisesRegex(frappe.ValidationError, "not a number this grain calls from"):
				bridge._caller_id(self.rule, self.account, number)

	def test_an_unpicked_number_sends_the_grains_default(self):
		"""A programmatic call states no preference, so the grain's own default is what the patient sees."""
		self.assertEqual(bridge._caller_id(self.rule, self.account, None), GRAIN_DID)

	def test_one_phone_on_two_grains_routes_by_the_record_it_was_called_from(self):
		"""The defect this replaces: the lead was found by phone, so the most recently edited one won."""
		mine = _lead("_Test Route Mine", config.GRAIN["vertical"])
		theirs = _lead("_Test Route Theirs", OTHER_VERTICAL)
		_rule_for(OTHER_VERTICAL, config.OTHER_ACCOUNT)

		self.assertEqual(bridge._route("CRM Lead", mine)[1].name, config.ACCOUNT)
		self.assertEqual(bridge._route("CRM Lead", theirs)[1].name, config.OTHER_ACCOUNT)
		frappe.db.set_value("CRM Lead", theirs, "lead_name", "_Test Route Theirs Edited")
		self.assertEqual(bridge._route("CRM Lead", mine)[1].name, config.ACCOUNT)

	def test_a_record_whose_grain_has_no_rule_is_refused(self):
		orphan = _lead("_Test Route Orphan", UNROUTED_VERTICAL)
		with self.assertRaisesRegex(frappe.ValidationError, "not on any telephony route"):
			bridge._route("CRM Lead", orphan)


def _lead(name, vertical):
	"""A lead on one grain, sharing its phone with the other — the shape the phone lookup could not tell apart."""
	if not frappe.db.exists("CRM Vertical", vertical):
		frappe.get_doc({"doctype": "CRM Vertical", "vertical_name": vertical}).insert(
			ignore_permissions=True  # authz-ok: tier-a — test fixture, runs as Administrator
		)
	doc = frappe.get_doc({
		"doctype": "CRM Lead", "lead_name": name, "first_name": name, "mobile_no": SHARED_PHONE,
		"custom_vertical": vertical,
	}).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture, runs as Administrator
	frappe.db.commit()
	return doc.name


def _rule_for(vertical, account):
	"""The second grain's rule, so the two records route to two accounts."""
	name = frappe.db.get_value("CRM Telephony Routing", {"vertical": vertical})
	if name:
		return name
	doc = frappe.get_doc({"doctype": "CRM Telephony Routing", "vertical": vertical, "telephony_account": account})
	doc.insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture, runs as Administrator
	frappe.db.commit()
	return doc.name
