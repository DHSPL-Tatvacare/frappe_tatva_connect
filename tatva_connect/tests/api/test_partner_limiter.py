# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The rate + volume limiter — does it actually honour the toggle and the values?

The limiter ships dormant. These tests turn it ON (mocking the toggle only, never the config) and
drive the REAL Redis token buckets, because the failures that matter are the ones an operator meets
the first time they switch it on:

  * the settings form is saved once and every limit silently becomes 0
  * 0 on a burst is not "unlimited" — it is a bucket that can never refill, so the API 429s forever
  * a request denied by one bucket still spends the other bucket's budget, and the daily window
    never heals it

The buckets are real (frappe.cache), so each test uses a unique partner key and flushes it.
"""
import unittest
from unittest.mock import patch

import frappe

from tatva_connect.api import _base

_ENFORCE = "tatva_connect.api._base.automation.is_enabled"


def _flush(*names):
	for n in names:
		frappe.cache.delete_value(f"partner_rl:{n}")


class TestPartnerLimiter(unittest.TestCase):
	"""A partner (a truthy mapping) is what the limiter charges; a sysmgr (falsy) is exempt."""

	def setUp(self):
		self.mapping = frappe._dict({"source": "T", "vertical": "V", "crm_group": "G", "program": None})
		self.g = f"test:global:{frappe.generate_hash(length=8)}"
		self.t = f"test:token:{frappe.generate_hash(length=8)}"
		self._keys = {self.g, self.t}
		_flush(*self._keys)

	def tearDown(self):
		_flush(*self._keys)

	def _charge(self, cost, grate, gburst, trate, tburst, window=60, token_key=None):
		"""Charge the pair -> (retry_after | None, per_token_remaining | None). `token_key` isolates a
		probe onto a FRESH per-token bucket: a bucket carries its spent tokens in Redis, so re-charging
		the same key with a bigger budget would still be denied by its own drained state."""
		tkey = token_key or self.t
		self._keys.add(tkey)
		return _base._bucket_pair(self.mapping, cost, self.g, grate, gburst, tkey, trate, tburst, window)

	# -- P11: config is explicit; no magic zero ------------------------------

	def test_the_doctype_defaults_match_the_code_defaults(self):
		"""DRIFT LOCK. Every knob carries its DEFAULT on the doctype, so the form pre-fills it and an
		operator who saves without touching a field persists the default -- not a 0 they never typed.
		If the two lists drift, one save of the settings form silently rewrites live policy."""
		meta = frappe.get_meta(_base._SETTINGS)
		for field, expected in _base.DEFAULTS.items():
			with self.subTest(field=field):
				df = meta.get_field(field)
				self.assertIsNotNone(df, f"{field} is in DEFAULTS but not on the doctype")
				self.assertEqual(
					str(df.default), str(expected),
					f"{field}: doctype default {df.default!r} != code DEFAULT {expected!r}. "
					f"A blank save would persist 0, and 0 on a rate means UNLIMITED.",
				)

	def test_a_blank_settings_save_cannot_disable_the_limiter(self):
		"""The failure this whole fix exists for: an operator saves the settings form having touched
		one field, and every other limit lands as 0 -- which on a rate means unlimited."""
		sp = "limiter_blank_save"
		frappe.db.savepoint(sp)
		try:
			frappe.db.delete("Singles", {"doctype": _base._SETTINGS})  # a fresh site
			frappe.clear_document_cache(_base._SETTINGS)
			doc = frappe.get_doc(_base._SETTINGS)
			doc.bulk_max_records = 50           # the operator touches exactly one field
			doc.save(ignore_permissions=True)
			frappe.clear_document_cache(_base._SETTINGS)

			cfg = _base._cfg()
			self.assertEqual(cfg["bulk_max_records"], 50, "the field the operator set must stick")
			for field in ("per_token_rate", "global_rate", "per_token_burst", "global_burst",
			              "per_token_read_records", "per_token_write_records"):
				with self.subTest(field=field):
					self.assertEqual(
						cfg[field], _base.DEFAULTS[field],
						f"{field} came back {cfg[field]} after a blank save -- the limiter is off",
					)
		finally:
			frappe.db.rollback(save_point=sp)
			frappe.clear_document_cache(_base._SETTINGS)

	def test_a_burst_of_zero_falls_back_to_the_default_and_never_bricks_the_api(self):
		"""A burst is a CAPACITY, not a dimension. It was in _UNLIMITED_WHEN_ZERO, so a 0 burst with a
		non-zero rate gave a bucket of capacity zero whose refill clamped to zero: permanent 429."""
		self.assertNotIn("per_token_burst", _base._UNLIMITED_WHEN_ZERO)
		self.assertNotIn("global_burst", _base._UNLIMITED_WHEN_ZERO)

		# Even if a 0 reaches the bucket, the Lua clamps burst up to the rate rather than deadlocking.
		for i in range(3):
			retry_after, _rem = self._charge(1, grate=0, gburst=0, trate=10, tburst=0)
			self.assertIsNone(retry_after, f"call {i + 1} was 429'd by a zero-capacity bucket")

	def test_zero_on_a_rate_means_unlimited_and_sends_no_headers(self):
		"""0 on a DIMENSION is an explicit 'unlimited'. It must not be reported as a budget of nothing."""
		for i in range(50):
			self.assertIsNone(self._charge(1, 0, 0, 0, 0)[0], "an unlimited dimension must never deny")

		frappe.local.response_headers = frappe._dict()
		with patch.object(_base, "_cfg", return_value={**_base.DEFAULTS, "per_token_rate": 0}):
			_base._ratelimit_headers(self.mapping, remaining=None)
		self.assertNotIn(
			"RateLimit-Limit", frappe.local.response_headers,
			"RateLimit-Limit: 0 reads to a client as an exhausted budget -- omit it instead",
		)

	# -- the limiter actually limits -----------------------------------------

	def test_the_per_token_bucket_denies_once_the_budget_is_spent(self):
		"""3 calls of burst, then the 4th is refused with a Retry-After."""
		for i in range(3):
			self.assertIsNone(self._charge(1, 1000, 1000, 3, 3)[0], f"call {i + 1} must be allowed")
		retry_after, remaining = self._charge(1, 1000, 1000, 3, 3)
		self.assertIsNotNone(retry_after, "the 4th call must be refused")
		self.assertGreater(retry_after, 0, "a refusal must say when to retry")
		self.assertEqual(remaining, 0)

	def test_cost_is_the_row_count_not_the_call_count(self):
		"""A bulk of 100 rows drains a 100-row budget in ONE call."""
		self.assertIsNone(self._charge(100, 10000, 10000, 100, 100)[0], "100 rows must fit a 100 budget")
		self.assertIsNotNone(self._charge(1, 10000, 10000, 100, 100)[0], "the budget is now spent")

	# -- P6: a denial by one bucket must not spend the other ------------------

	def test_a_denial_by_one_bucket_does_not_spend_the_others_budget(self):
		"""THE LEAK. The old script debited the global bucket, THEN tested the per-token one. A partner
		at their own ceiling burned rows from the SHARED global bucket on every rejected attempt, and
		the volume window is a day, so it never healed -- the global ceiling ratcheted down until it
		denied every partner."""
		# Partner A: a per-token budget of 5 against a shared global budget of 1000. Spend the 5.
		for _ in range(5):
			self.assertIsNone(self._charge(1, 1000, 1000, 5, 5)[0])

		# 20 more attempts, every one denied by A's OWN bucket. The global must pay nothing for these.
		for i in range(20):
			self.assertIsNotNone(self._charge(1, 1000, 1000, 5, 5)[0], f"attempt {i + 1} must be denied")

		# Probe the shared global bucket through a FRESH partner (its own bucket, huge budget), so the
		# only thing that can deny is the global ceiling. 1000 - 5 spent = 995 must remain.
		probe = f"test:token:probe:{frappe.generate_hash(length=8)}"
		self.assertIsNone(
			self._charge(995, 1000, 1000, 10_000, 10_000, token_key=probe)[0],
			"the shared global bucket was short: the 20 requests denied by A's own bucket "
			"spent the global budget anyway -- that is the leak, and a daily window never heals it",
		)
		self.assertIsNotNone(
			self._charge(1, 1000, 1000, 10_000, 10_000, token_key=probe)[0],
			"the global bucket should now be exactly empty (5 + 995 = 1000)",
		)

	# -- exemption ------------------------------------------------------------

	def test_a_caller_with_no_mapping_is_exempt(self):
		"""A trusted System Manager (no mapping row) is never charged."""
		self.assertIsNone(_base._bucket_pair(None, 10_000, self.g, 1, 1, self.t, 1, 1, 60))

	def test_the_limiter_is_dormant_until_the_toggle_is_on(self):
		"""Ships OFF: with the switch dormant, no endpoint charges anything."""
		with patch(_ENFORCE, return_value=False):
			self.assertIsNone(_base._meter_volume(10 ** 9, "read"), "a dormant limiter must not charge")

	def test_redis_failure_fails_open(self):
		"""A limiter that cannot reach Redis must ALLOW the request, not lock the partner out."""
		with patch.object(_base.frappe.cache, "evalsha", side_effect=RuntimeError("redis down")):
			self.assertIsNone(
				_base._bucket_pair(self.mapping, 1, self.g, 1, 1, self.t, 1, 1, 60),
				"a broken limiter must fail OPEN",
			)
