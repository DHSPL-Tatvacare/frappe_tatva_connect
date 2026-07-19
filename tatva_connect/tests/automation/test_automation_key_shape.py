# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Gate: every automation key is `Area::Subject::Capability` — exactly three non-empty `::` parts.

Nothing enforced the shape, and it drifted: a rekey landed `WhatsApp::messaging`,
`WhatsApp::templates` and `WhatsApp::backfill` — three two-part keys among 40-odd three-part ones.
Nothing failed, because nothing looked. The shape is not cosmetic: `Area` is segment 1 (seed.py reads
`key.split("::")[0]` into the `area` column), and the go-live checklist groups an operator's switches
by that same prefix, so a short key files itself under the wrong heading and an operator misses it.

Two seams are covered, because there are two writers: the registry (`Auto.__post_init__`, which fails
at IMPORT so a bad key can never reach the catalog) and the doctype controller (`validate`, for a row
written straight to the DB). Plus a sweep of every key really registered on the site, so future drift
fails here rather than surviving unnoticed.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import seed
from tatva_connect.automation.registry import AUTOMATIONS, Auto, assert_valid_key

_TWO_PART = "WhatsApp::messaging"
_THREE_PART = "WhatsApp::Channel::messaging"


class TestAutomationKeyShape(FrappeTestCase):
	# --- the shape check itself ------------------------------------------

	def test_a_two_part_key_is_rejected(self):
		"""The exact drift that got through: two parts, no Subject."""
		with self.assertRaises(ValueError):
			assert_valid_key(_TWO_PART)

	def test_a_three_part_key_is_accepted(self):
		self.assertEqual(assert_valid_key(_THREE_PART), _THREE_PART)

	def test_the_other_malformed_shapes_are_rejected(self):
		"""One part, four parts, and an empty segment are all outside the contract."""
		for bad in ("wati", "WhatsApp::Channel::messaging::extra", "WhatsApp::::messaging", "", None):
			with self.subTest(key=bad), self.assertRaises(ValueError):
				assert_valid_key(bad)

	# --- seam 1: the registry --------------------------------------------

	def test_the_registry_refuses_to_build_a_two_part_key(self):
		"""A malformed key cannot reach AUTOMATIONS — construction itself raises."""
		with self.assertRaises(ValueError):
			Auto(key=_TWO_PART, fires_on="Provider call")

	def test_the_registry_refuses_a_two_part_requires(self):
		"""`requires` names another row by key, so it obeys the same shape."""
		with self.assertRaises(ValueError):
			Auto(key=_THREE_PART, fires_on="Provider call", requires=_TWO_PART)

	# --- seam 2: the doctype controller ----------------------------------

	def test_the_controller_refuses_a_two_part_row(self):
		"""A row written straight to the DB is gated too — the registry is not the only writer."""
		doc = frappe.new_doc("CRM Tatva Automation")
		doc.automation_key = "Drift::twopart"
		doc.fires_on = "Doc Event"
		with self.assertRaises(frappe.ValidationError):
			doc.insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user context

	def test_the_controller_accepts_a_three_part_row(self):
		"""The same insert with a Subject segment goes in — proving the guard bites on shape only."""
		doc = frappe.new_doc("CRM Tatva Automation")
		doc.automation_key = "Drift::Guard::proof"
		doc.fires_on = "Doc Event"
		doc.insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user context
		self.assertTrue(frappe.db.exists("CRM Tatva Automation", "Drift::Guard::proof"))
		frappe.delete_doc("CRM Tatva Automation", "Drift::Guard::proof", force=True, ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user context

	# --- the sweep: every key on the site --------------------------------

	def test_every_registered_key_is_three_part(self):
		"""The catalog in code. A future two-part key fails CI here."""
		for auto in AUTOMATIONS:
			with self.subTest(key=auto.key):
				assert_valid_key(auto.key)

	def test_every_key_on_the_site_is_three_part(self):
		"""The rows really on the site — the registry can be clean while the DB still carries a
		pre-rename row that the rekey patch was meant to have moved."""
		seed.sync_catalog()
		for name in frappe.get_all("CRM Tatva Automation", pluck="name"):
			with self.subTest(key=name):
				assert_valid_key(name)

	def test_the_whatsapp_switches_carry_the_channel_subject(self):
		"""The specific drift, pinned: these three gate the CHANNEL, so `Channel` is the Subject —
		deliberately not the vendor-scoped `WhatsApp::WATI::*` they replaced."""
		from tatva_connect.whatsapp import channel

		for key in (channel.SWITCH_MESSAGING, channel.SWITCH_TEMPLATES, channel.SWITCH_BACKFILL):
			with self.subTest(key=key):
				assert_valid_key(key)
				self.assertEqual(key.split("::")[:2], ["WhatsApp", "Channel"])
				self.assertTrue(
					frappe.db.exists("CRM Tatva Automation", key),
					f"{key} must exist as a row, or the switch gates nothing",
				)
