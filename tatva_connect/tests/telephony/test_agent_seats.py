"""A rep's extension belongs to one account: an outbound call rings the extension on the lead's account, never another."""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.telephony import bridge, resolve
from tatva_connect.telephony.adapters import acefone
from tatva_connect.tests.telephony.fixtures import config

REP = "_test_tel_seat_rep@example.com"
OTHER_REP = "_test_tel_seat_other@example.com"
SEAT = "0600000000101"
OTHER_SEAT = "0600000000202"


class _Refusing:
	"""An adapter that always says no, with a plain provider message."""

	@staticmethod
	def token_rejected(resp):
		return False


class TestAgentSeats(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		config.ensure_account(rep_emails=(REP, OTHER_REP))
		config.ensure_account(account=config.OTHER_ACCOUNT)

	def setUp(self):
		config.clear_seats()
		config.clear_seats(config.OTHER_ACCOUNT)

	def tearDown(self):
		frappe.set_user("Administrator")
		config.clear_seats()
		config.clear_seats(config.OTHER_ACCOUNT)

	def _ring(self, account):
		frappe.set_user(REP)
		return bridge._agent_number(frappe.get_doc(acefone.ACCOUNT_DT, account))

	def test_each_account_rings_its_own_seat(self):
		config.set_seat(REP, SEAT)
		config.set_seat(REP, OTHER_SEAT, account=config.OTHER_ACCOUNT)
		self.assertEqual(self._ring(config.ACCOUNT), SEAT)
		self.assertEqual(self._ring(config.OTHER_ACCOUNT), OTHER_SEAT)

	def test_a_seat_on_another_account_is_never_rung(self):
		config.set_seat(REP, SEAT, account=config.OTHER_ACCOUNT)
		with self.assertRaisesRegex(frappe.ValidationError, "no extension on"):
			self._ring(config.ACCOUNT)

	def test_a_rep_has_one_extension_per_account(self):
		config.set_seat(REP, SEAT)
		doc = frappe.get_doc(resolve.AGENT_DOCTYPE, REP)
		doc.append(resolve.SEAT_FIELD, {"telephony_account": config.ACCOUNT, "extension": OTHER_SEAT})
		self.assertRaisesRegex(frappe.ValidationError, "more than one extension", doc.save)

	def test_an_extension_on_an_account_belongs_to_one_rep(self):
		config.set_seat(REP, SEAT)
		with self.assertRaisesRegex(frappe.ValidationError, "already belongs to"):
			config.set_seat(OTHER_REP, SEAT)
		config.set_seat(OTHER_REP, SEAT, account=config.OTHER_ACCOUNT)

	def test_a_provider_refusal_is_logged_and_shown(self):
		row = bridge._new_call_log("919000000001", SEAT, config.ACCOUNT, None, None, bridge.MEDIUM, caller=REP)
		resp = {"success": False, "message": "Agent is not available"}
		with self.assertRaisesRegex(frappe.ValidationError, "Agent is not available"):
			bridge._refuse(row, resp, _Refusing, acefone.PROVIDER)
		logged = frappe.get_all("Error Log", filters={"reference_name": row.name}, pluck="error")
		self.assertEqual(len(logged), 1)
		self.assertIn("Agent is not available", logged[0])
		frappe.delete_doc("CRM Call Log", row.name, force=True, ignore_permissions=True)
