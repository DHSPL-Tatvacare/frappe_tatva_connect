# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""P2 gave the spotlight's permission read a per-user TTL cache. P4a replaced WHAT is read: the cache used to
hold `visible_lead_names()` — every lead the caller may see, unbounded by design — and now holds
`visible_principals()`, the caller's own line as principal tokens, bounded by HEADCOUNT. A cheaper wrong
design was still wrong; the cache itself was right and survives unchanged.

So this suite still asserts the cache (one read per user per window, per-user keys, a real expiry, and the
cross-user isolation that matters), and additionally asserts the thing that changed: the cached payload
contains no lead id at all.

`redis_cache` needs a module-level function — it builds its key from the call arguments and `self` is not
stably hashable — and `user=True` because the framework's `make_key` already prefixes `user:<session user>:`.
No key is hand-rolled.

The personas are plain Sales Users on purpose: a Sales Manager outside the org tree is EXEMPT (org_hierarchy
narrows them by nothing), so there would be nothing to cache. That exemption is asserted here too, and in
full in `test_permission_predicate.py`.

This mints its own verticals, users, User Permissions and leads rather than reading a site's seed, so it
asserts the CODE and not somebody's data.

Run:
    bench --site uatreplay.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.search.test_permission_cache
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.search import index as search_index
from tatva_connect.search.index import CRMLeadSearch

VERTICAL_A = "ZZ Perm Cache A"
VERTICAL_B = "ZZ Perm Cache B"
GROUP = "ZZ Perm Cache Group"
USER_A = "zz-perm-cache-a@example.com"
USER_B = "zz-perm-cache-b@example.com"
USER_MGR = "zz-perm-cache-mgr@example.com"
PHONE_PREFIX = "+91610006"


class TestPermissionCache(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._purge()
		for vertical in (VERTICAL_A, VERTICAL_B):
			if not frappe.db.exists("CRM Vertical", vertical):
				frappe.get_doc({"doctype": "CRM Vertical", "vertical_name": vertical}).insert(ignore_permissions=True)
		if not frappe.db.exists("CRM Group", GROUP):
			frappe.get_doc({"doctype": "CRM Group", "group_name": GROUP}).insert(ignore_permissions=True)

		# Sales User, so org_hierarchy really narrows them and the cache has a bounded read to hold.
		cls._user(USER_A, "Sales User", VERTICAL_A)
		cls._user(USER_B, "Sales User", VERTICAL_B)
		# Sales Manager outside the org tree — the exempt persona.
		cls._user(USER_MGR, "Sales Manager", VERTICAL_A)

		cls.lead_a = cls._lead(1, VERTICAL_A, USER_A)
		cls.lead_b = cls._lead(2, VERTICAL_B, USER_B)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		cls._purge()
		for email in (USER_A, USER_B, USER_MGR):
			frappe.db.delete("User Permission", {"user": email})
			if frappe.db.exists("User", email):
				frappe.delete_doc("User", email, force=True, ignore_permissions=True)
		for dt, name in (("CRM Group", GROUP), ("CRM Vertical", VERTICAL_A), ("CRM Vertical", VERTICAL_B)):
			if frappe.db.exists(dt, name):
				frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	@classmethod
	def _purge(cls):
		for name in frappe.get_all("CRM Lead", filters={"mobile_no": ["like", f"{PHONE_PREFIX}%"]}, pluck="name"):
			frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)

	@classmethod
	def _user(cls, email, role, vertical):
		if not frappe.db.exists("User", email):
			user = frappe.get_doc({
				"doctype": "User", "email": email, "first_name": "Perm Cache",
				"send_welcome_email": 0, "user_type": "System User",
			}).insert(ignore_permissions=True)
			user.append("roles", {"role": role})
			user.save(ignore_permissions=True)
		if not frappe.db.exists("User Permission", {"user": email, "allow": "CRM Vertical"}):
			frappe.get_doc({
				"doctype": "User Permission", "user": email,
				"allow": "CRM Vertical", "for_value": vertical, "applicable_for": "CRM Lead",
			}).insert(ignore_permissions=True)

	@classmethod
	def _lead(cls, seq, vertical, owner):
		doc = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Perm Cache", "mobile_no": f"{PHONE_PREFIX}{seq:04d}",
			"status": "New", "custom_vertical": vertical, "custom_group": GROUP, "lead_owner": owner,
		}).insert(ignore_permissions=True)
		frappe.db.set_value("CRM Lead", doc.name, "lead_owner", owner, update_modified=False)
		return doc.name

	def setUp(self):
		self.addCleanup(frappe.set_user, "Administrator")
		for email in (USER_A, USER_B, USER_MGR, "Administrator"):
			self._clear_cache_as(email)

	# --- helpers ------------------------------------------------------------------------------------

	def _clear_cache_as(self, email):
		"""The framework's own invalidation, run under each persona — `clear_cache` keys on the session user."""
		frappe.set_user(email)
		search_index.visible_principals.clear_cache()
		frappe.set_user("Administrator")

	def _cache_keys(self):
		"""The live Redis keys for this function under the CURRENT session user, via frappe's own make_key."""
		fn = search_index.visible_principals
		return frappe.cache.get_keys(f"{fn.__module__}.{fn.__qualname__}", user=True)

	def _filters_as(self, email):
		frappe.set_user(email)
		try:
			return CRMLeadSearch().get_search_filters()
		finally:
			frappe.set_user("Administrator")

	def _counting_condition_reads(self):
		"""Count the reads the cache exists to avoid — one per search on the old code, i.e. one PER KEYSTROKE."""
		real = search_index.get_lead_permission_query_conditions
		calls = []

		def counted(*args, **kwargs):
			calls.append(args)
			return real(*args, **kwargs)

		search_index.get_lead_permission_query_conditions = counted
		self.addCleanup(setattr, search_index, "get_lead_permission_query_conditions", real)
		return calls

	# --- performance --------------------------------------------------------------------------------

	def test_two_searches_by_one_user_inside_the_ttl_cost_one_permission_read(self):
		calls = self._counting_condition_reads()
		first = self._filters_as(USER_A)
		second = self._filters_as(USER_A)
		self.assertEqual(len(calls), 1, "a second search inside the TTL must not re-read the permission rule")
		self.assertEqual(first, second)
		self.assertEqual(first["principals"], ["LIKE", ["|everyone|", f"|{USER_A}|"]])

	# --- the payload is BOUNDED (what P4a changed) --------------------------------------------------

	def test_the_cached_payload_holds_principals_and_not_one_lead_id(self):
		frappe.set_user(USER_A)
		try:
			payload = search_index.visible_principals()
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(payload, ["|everyone|", f"|{USER_A}|"])
		self.assertNotIn(self.lead_a, payload)
		self.assertNotIn(self.lead_b, payload)

	# --- isolation (the security assertion) ---------------------------------------------------------

	def test_one_users_cache_never_answers_another_users_search(self):
		calls = self._counting_condition_reads()
		a_first = self._filters_as(USER_A)
		b_first = self._filters_as(USER_B)
		a_again = self._filters_as(USER_A)
		b_again = self._filters_as(USER_B)

		for filters in (a_first, a_again):
			self.assertEqual(filters["principals"][1], ["|everyone|", f"|{USER_A}|"], "user A was served another line")
			self.assertNotIn(f"|{USER_B}|", filters["principals"][1])
		for filters in (b_first, b_again):
			self.assertEqual(filters["principals"][1], ["|everyone|", f"|{USER_B}|"], "user B was served another line")
			self.assertNotIn(f"|{USER_A}|", filters["principals"][1])
		# B's read is a MISS after A's — the entry is not shared — while each user's repeat is a HIT.
		self.assertEqual(len(calls), 2, "expected exactly one read per user, and no third read")

	def test_each_user_owns_a_separate_cache_entry(self):
		self._filters_as(USER_A)
		self._filters_as(USER_B)
		frappe.set_user(USER_A)
		keys_a = self._cache_keys()
		frappe.set_user(USER_B)
		keys_b = self._cache_keys()
		frappe.set_user("Administrator")
		self.assertEqual(len(keys_a), 1)
		self.assertEqual(len(keys_b), 1)
		self.assertNotEqual(keys_a[0], keys_b[0])
		self.assertIn(USER_A.encode(), keys_a[0])
		self.assertIn(USER_B.encode(), keys_b[0])

	# --- expiry -------------------------------------------------------------------------------------

	def test_the_entry_carries_the_declared_expiry(self):
		"""No sleep(60): the expiry asserted is the one REDIS really recorded."""
		self._filters_as(USER_A)
		frappe.set_user(USER_A)
		ttl = frappe.cache.ttl(self._cache_keys()[0])
		frappe.set_user("Administrator")
		self.assertGreater(ttl, 0, "the entry must expire, not live forever")
		self.assertLessEqual(ttl, search_index._PERMISSION_TTL)

	# --- the exempt personas ------------------------------------------------------------------------

	def test_a_sales_manager_outside_the_hierarchy_is_unscoped(self):
		self.assertEqual(self._filters_as(USER_MGR), {})
		frappe.set_user(USER_MGR)
		try:
			self.assertEqual(search_index.visible_principals(), [], "an exempt caller must name no principal")
		finally:
			frappe.set_user("Administrator")

	def test_a_system_manager_is_unscoped(self):
		self.assertEqual(self._filters_as("Administrator"), {})
