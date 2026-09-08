# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""THE PICKER OFFERS WHAT THIS NUMBER CAN ACTUALLY SEND.

MEASURED ON THE LIVE PROVIDER, 2026-09-08. One provider account, two numbers:

    read without naming a number ... 147 templates, the SAME list for both
    read as 918511975757 .......... 235
    read as 919974306678 ...........  94, a strict subset of the 235

Templates are approved per NUMBER. Read without naming one, both numbers mirrored the same 147 rows, so
a rep on the smaller number was offered 141 templates it cannot send — and the provider would have
refused them in front of a patient rather than in the picker, because a refusal only happens at send.

TWO HALVES, AND THE SECOND IS THE ONE THAT BITES. Reading per channel fixes what arrives; it does not
fix what is already stored, because the mirror only ever added and updated. The 53 rows the smaller
number could no longer send stayed in its picker until they were retired.

RETIRED, NOT DELETED: the picker offers `APPROVED` only, so retiring hides the row at once while the
operator's {{N}} -> CRM field mapping on it survives the template being approved again.

Hermetic: the provider read is intercepted, so what is asserted is the catalogue this app builds and
stores from a given answer. No network.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.whatsapp.test_template_catalogue
"""
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.whatsapp import templates_sync, transport, wati

_ACCOUNT = "Catalogue-probe-account"
_NUMBER = "919900000661"


def _v3_template(name, status="APPROVED", params=None):
	"""One item shaped as the v3 catalogue really returns it — snake_case, `name`, `custom_params`."""
	return {
		"id": f"id-{name}",
		"name": name,
		"status": status,
		"category": "UTILITY",
		"body": "Hello {{1}}",
		"language_option": {"key": "en", "value": "en", "text": "English"},
		"custom_params": params or [],
	}


class TestTheCatalogueIsPerNumber(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if frappe.db.exists("WhatsApp Account", _ACCOUNT):
			frappe.delete_doc("WhatsApp Account", _ACCOUNT, force=True, ignore_permissions=True)
		cls.account = frappe.get_doc({
			"doctype": "WhatsApp Account", "account_name": _ACCOUNT, "status": "Active",
			"url": "https://live-mt-server.wati.io/000661", "token": "catalogue-token",
			"custom_provider": "WATI", "custom_wati_channel_number": _NUMBER,
			"custom_wati_multi_number": 1,
		}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.db.delete("WhatsApp Templates", {"whatsapp_account": _ACCOUNT})
		frappe.delete_doc("WhatsApp Account", _ACCOUNT, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		self.addCleanup(self._clear)
		self._clear()

	def _clear(self):
		frappe.db.delete("WhatsApp Templates", {"whatsapp_account": _ACCOUNT})
		frappe.db.commit()

	def _sync(self, items):
		with mock.patch.object(transport, "get_message_templates", return_value=items):
			return templates_sync.sync_templates(_ACCOUNT)

	def _stored(self, status):
		return {
			frappe.db.get_value("WhatsApp Templates", row, "actual_name")
			for row in frappe.get_all(
				"WhatsApp Templates",
				filters={"whatsapp_account": _ACCOUNT, "status": status}, pluck="name",
			)
		}

	# ===================================================================== The read names its number =====================================================================
	def test_the_catalogue_is_read_as_the_number_the_send_will_leave_from(self):
		"""Asked without a channel, the provider answers with a list that is not this number's."""
		asked = {}

		def _read(_account, channel_number="", **_kwargs):
			asked["channel"] = channel_number
			return []

		with mock.patch.object(transport, "get_message_templates", _read):
			wati.list_templates(self.account)
		self.assertEqual(asked["channel"], _NUMBER)

	# ===================================================================== The provider's dialect stops at the adapter =====================================================================
	def test_a_v3_item_becomes_the_channels_own_shape(self):
		shaped = wati._template_shape(
			_v3_template("welcome", params=[{"name": "1", "value": "Asha"}])
		)
		self.assertEqual(shaped["name"], "welcome")
		self.assertEqual(shaped["language"], "en")
		self.assertEqual(shaped["body"], "Hello {{1}}")
		self.assertEqual(shaped["variables"], {"1": "Asha"})

	def test_a_template_that_is_not_approved_is_never_offered(self):
		"""A draft or rejected template answers the provider's refusal at the patient, not the screen."""
		self.assertIsNone(wati._template_shape(_v3_template("pending_one", status="PENDING")))

	# ===================================================================== The mirror follows the provider DOWN as well as up =====================================================================
	def test_a_template_the_number_can_no_longer_send_is_retired(self):
		"""THE red. The mirror only added and updated, so a template dropped from this number's catalogue
		stayed in its picker — offering a rep something the provider would refuse at send."""
		self._sync([_v3_template("keeps"), _v3_template("drops")])
		self.assertEqual(self._stored("APPROVED"), {"keeps", "drops"})

		self._sync([_v3_template("keeps")])
		self.assertEqual(self._stored("APPROVED"), {"keeps"})
		self.assertEqual(self._stored("RETIRED"), {"drops"})

	def test_retiring_keeps_the_row_so_an_operators_mapping_survives(self):
		"""Not deleted: the {{N}} -> CRM field mapping is the operator's work, and a template the
		provider restores must come back with it rather than blank."""
		self._sync([_v3_template("drops")])
		name = frappe.get_all(
			"WhatsApp Templates", filters={"whatsapp_account": _ACCOUNT}, pluck="name"
		)[0]
		frappe.db.set_value("WhatsApp Templates", name, "field_names", "custom_patient_id")

		self._sync([])
		self.assertEqual(frappe.db.get_value("WhatsApp Templates", name, "status"), "RETIRED")
		self.assertEqual(
			frappe.db.get_value("WhatsApp Templates", name, "field_names"), "custom_patient_id"
		)

	def test_a_retired_template_comes_back_when_the_provider_lists_it_again(self):
		self._sync([_v3_template("returns")])
		self._sync([])
		self.assertEqual(self._stored("RETIRED"), {"returns"})

		self._sync([_v3_template("returns")])
		self.assertEqual(self._stored("APPROVED"), {"returns"})
		self.assertEqual(self._stored("RETIRED"), set())

	def test_a_second_sync_over_the_same_catalogue_retires_nothing(self):
		"""Idempotent: only rows the provider stopped listing are touched, so a scheduled re-sync of an
		unchanged catalogue writes no retirement at all."""
		self._sync([_v3_template("a"), _v3_template("b")])
		self._sync([_v3_template("a"), _v3_template("b")])
		self.assertEqual(self._stored("RETIRED"), set())
		self.assertEqual(self._stored("APPROVED"), {"a", "b"})
