"""A call's details carry its provider's logo, uploaded on that provider's account, whichever channel carried it.

Run:
    bench --site dev.localhost run-tests --app tatva_connect --module tatva_connect.tests.storage.test_a_call_shows_its_provider_logo
"""
import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime

from tatva_connect.telephony import bridge

PROVIDER = "bolna"
LOGO = "/files/_test_provider_logo.png"


class TestProviderLogo(IntegrationTestCase):
	def test_a_call_carries_its_providers_logo_and_its_name_without_one(self):
		call = frappe.get_doc({"doctype": "CRM Call Log", "id": frappe.generate_hash(length=12), "type": "Outgoing",
		                       "status": "Completed", "from": "9000000001", "to": "+919000000402",
		                       "telephony_medium": "AI Voice", "start_time": now_datetime()}).insert(ignore_permissions=True)
		frappe.get_doc({"doctype": "CRM Call Media", "call": call.name, "recording_source": PROVIDER}).insert(ignore_permissions=True)
		frappe.db.delete("CRM AI Voice Account", {"provider": PROVIDER})  # rolled back with the class; this test owns the provider's accounts
		self.assertEqual(bridge.get_call_log(call.name)["_provider"], {"name": PROVIDER, "logo": None})

		frappe.get_doc({"doctype": "CRM AI Voice Account", "account_name": "_Test Voice Logo", "provider": PROVIDER,
		                "api_key": "x", "logo": LOGO}).insert(ignore_permissions=True)
		self.assertEqual(bridge.get_call_log(call.name)["_provider"], {"name": PROVIDER, "logo": LOGO})
