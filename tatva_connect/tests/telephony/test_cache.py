"""The cached reads: fast on the second look, and current the moment an operator saves."""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.telephony import bridge, cache, resolve
from tatva_connect.telephony.adapters import acefone
from tatva_connect.tests.telephony.fixtures import config

REP = "_test_tel_cache_rep@example.com"
SEAT = "0600000000301"
NEW_SEAT = "0600000000302"
DID = "9000888111"
NEW_DID = "9000888222"


class TestTelephonyCache(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		config.ensure_account(rep_emails=(REP,))

	def setUp(self):
		config.clear_dids()
		config.clear_seats()
		cache.invalidate()

	def tearDown(self):
		config.clear_dids()
		config.clear_seats()

	def _rule(self):
		rule = config.routing_rule()
		return frappe._dict(name=rule.name, telephony_account=rule.telephony_account)

	def test_a_seat_read_is_served_from_the_cache_next_time(self):
		config.set_seat(REP, SEAT)
		self.assertIsNone(frappe.cache.get_value(f"{cache.NAMESPACE}:seats:{config.ACCOUNT}"))
		self.assertEqual(resolve.seat_for_user(REP, config.ACCOUNT), SEAT)
		self.assertIsNotNone(frappe.cache.get_value(f"{cache.NAMESPACE}:seats:{config.ACCOUNT}"))

	def test_saving_an_agent_shows_the_new_extension_at_once(self):
		config.set_seat(REP, SEAT)
		self.assertEqual(resolve.seat_for_user(REP, config.ACCOUNT), SEAT)
		config.set_seat(REP, NEW_SEAT)
		self.assertEqual(resolve.seat_for_user(REP, config.ACCOUNT), NEW_SEAT)
		self.assertEqual(resolve.user_for_seat(NEW_SEAT, config.ACCOUNT), REP)
		self.assertIsNone(resolve.user_for_seat(SEAT, config.ACCOUNT))

	def test_saving_a_routing_rule_shows_the_new_number_at_once(self):
		config.map_did(DID)
		account = frappe.get_doc(acefone.ACCOUNT_DT, config.ACCOUNT)
		self.assertEqual([n.did_number for n in bridge._caller_pool(self._rule(), account)], [DID])
		config.map_did(NEW_DID)
		self.assertEqual(
			[n.did_number for n in bridge._caller_pool(self._rule(), account)], [DID, NEW_DID]
		)
