# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
# TEMPORARY — migration reconciliation demo, remove with tatva_connect/migration_check/.
"""THE ONLY THING PROTECTING PRODUCTION FROM THIS TOOL.

Migration Check is a temporary demo that reads LeadSquared with stored credentials. It must be
inert anywhere it was not deliberately switched on. That is enforced entirely by config: the code
may ship anywhere, but without `migration_check_enabled` AND the current site named in
`migration_check_allowed_sites`, every entry point refuses.

If this test goes red, the tool is live somewhere nobody chose to make it live.

Two more locks live here because both are silent when wrong:
  * the LeadSquared read-only allowlist — "read only" is an endpoint allowlist, not an HTTP verb,
    since LeadSquared's retrieve APIs are POST;
  * the grain contract — each grain is a separate LeadSquared account with its own agreed codes,
    so a grain that quietly loses its map would compare a lead against the wrong rules.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.migration_check import constants as C
from tatva_connect.migration_check import guard, lsq

_KEYS = ("migration_check_enabled", "migration_check_allowed_sites", "migration_check_accounts")

_FAKE = {
	"lsq_host": "https://example.invalid",
	"lsq_access_key": "k",
	"lsq_secret_key": "s",
	"partner_token": "a:b",
}


class TestMigrationCheckGuard(FrappeTestCase):
	def setUp(self):
		self._saved = {k: frappe.conf.get(k) for k in _KEYS}

	def tearDown(self):
		for key, value in self._saved.items():
			if value is None:
				frappe.conf.pop(key, None)
			else:
				frappe.conf[key] = value

	def _enable(self, sites=None, accounts=None):
		frappe.conf["migration_check_enabled"] = 1
		frappe.conf["migration_check_allowed_sites"] = [frappe.local.site] if sites is None else sites
		frappe.conf["migration_check_accounts"] = {"anaya": dict(_FAKE)} if accounts is None else accounts

	def test_refuses_when_flag_absent(self):
		frappe.conf.pop("migration_check_enabled", None)
		frappe.conf["migration_check_allowed_sites"] = [frappe.local.site]
		with self.assertRaises(guard.NotEnabled):
			guard.assert_permitted()

	def test_refuses_when_flag_off(self):
		self._enable()
		frappe.conf["migration_check_enabled"] = 0
		with self.assertRaises(guard.NotEnabled):
			guard.assert_permitted()

	def test_refuses_when_site_not_allowed(self):
		"""The flag alone is not enough — a copied config must not switch it on elsewhere."""
		self._enable(sites=["some-other-site.example.com"])
		with self.assertRaises(guard.NotEnabled):
			guard.assert_permitted()

	def test_refuses_when_allowlist_empty(self):
		self._enable(sites=[])
		with self.assertRaises(guard.NotEnabled):
			guard.assert_permitted()

	def test_refuses_when_no_grain_configured(self):
		self._enable(accounts={})
		with self.assertRaises(guard.NotEnabled):
			guard.assert_permitted()

	def test_grain_missing_a_credential_is_not_offered(self):
		"""A half-configured grain must not appear in the picker, or it fails mid-lookup."""
		partial = dict(_FAKE)
		partial.pop("partner_token")
		self._enable(accounts={"anaya": dict(_FAKE), "tatvapractice": partial})
		self.assertEqual([g["slug"] for g in guard.available_grains()], ["anaya"])

	def test_credentials_are_per_grain(self):
		self._enable(accounts={"anaya": dict(_FAKE, lsq_access_key="AAA")})
		self.assertEqual(guard.credentials("anaya").lsq_access_key, "AAA")
		with self.assertRaises(guard.NotEnabled):
			guard.credentials("tatvapractice")

	def test_allows_when_fully_configured(self):
		self._enable()
		guard.assert_permitted()
		self.assertTrue(guard.available_grains())

	def test_refuses_guest_even_when_enabled(self):
		self._enable()
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				guard.assert_permitted()
		finally:
			frappe.set_user("Administrator")

	def test_is_available_never_raises(self):
		"""The template calls this to decide what to render; it must not explode."""
		frappe.conf.pop("migration_check_enabled", None)
		self.assertFalse(guard.is_available())


class TestLSQReadOnlyAllowlist(FrappeTestCase):
	def test_allowlist_holds_only_read_endpoints(self):
		"""A write endpoint here would be reachable with real credentials."""
		banned = ("create", "update", "capture", "delete", "post", "modify", "import")
		for endpoint in lsq.READ_ONLY_ENDPOINTS:
			lowered = endpoint.lower()
			for word in banned:
				self.assertNotIn(word, lowered, f"{endpoint} looks like a write endpoint")

	def test_non_allowlisted_endpoint_never_reaches_the_network(self):
		client = lsq.Client("https://example.invalid", "key", "secret")
		try:
			with self.assertRaises(lsq.ReadOnlyViolation):
				client._call("LeadManagement.svc/Lead.Create")
		finally:
			client.close()


class TestGrainContract(FrappeTestCase):
	def test_every_grain_is_complete(self):
		"""A grain with no codes or no label would silently compare against nothing."""
		self.assertTrue(C.GRAINS, "grains.json is empty")
		for slug in C.GRAINS:
			grain = C.Grain(slug)
			self.assertTrue(grain.label, f"{slug} has no label")
			self.assertTrue(grain.vertical and grain.group, f"{slug} has no grain axes")
			self.assertTrue(grain.mapped_event_codes, f"{slug} migrates no event codes")
			self.assertIn("activities", grain.compares)

	def test_calls_row_only_where_calls_migrate(self):
		"""Showing 0 vs 0 for a grain that migrates no calls would imply calls were expected."""
		for slug in C.GRAINS:
			grain = C.Grain(slug)
			self.assertEqual(
				"calls" in grain.compares,
				bool(grain.call_log_events),
				f"{slug}: calls row and call_log_events disagree",
			)

	def test_grain_axes_are_unique(self):
		"""for_lead() reverse-maps a lead's grain, so two accounts may not share vertical+group."""
		axes = [(C.Grain(s).vertical, C.Grain(s).group) for s in C.GRAINS]
		self.assertEqual(len(axes), len(set(axes)), "two grains share the same vertical + group")

	def test_reverse_lookup_round_trips(self):
		for slug in C.GRAINS:
			grain = C.Grain(slug)
			self.assertEqual(C.for_lead(grain.vertical, grain.group), slug)

	def test_event_catalogue_names_every_code(self):
		for slug in C.GRAINS:
			catalogue = C.Grain(slug).event_catalogue()
			self.assertEqual(len(catalogue), len(C.Grain(slug).mapped_event_codes))
			for row in catalogue:
				self.assertTrue(row["becomes"], f"{slug}:{row['code']} has no Frappe target")
				self.assertIn(row["lands_in"], ("Activity", "Call log"))
