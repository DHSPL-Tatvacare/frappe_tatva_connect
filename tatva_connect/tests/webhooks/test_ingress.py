"""The inbound webhook gate — token, rotation, HMAC, IP allowlist.

Every future integration authenticates through `webhooks.ingress`, so these are the tests that keep
the front door honest for providers that do not exist yet.
"""
import base64
import hashlib
import hmac

import frappe
from frappe.tests.utils import FrappeTestCase
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from tatva_connect.webhooks import ingress, registry

ACCOUNT = "_TestIngressAcct"
TOKEN = "_test-ingress-token-9f4c2b7e1a8d"
SECRET = "_test-signing-secret"
BODY = b'{"call_id":"x","direction":"inbound"}'


class _Request:
	"""The minimum of a werkzeug request that the gate reads."""

	def __init__(self, token=None, body=b"{}", headers=None):
		self.args = {"token": token} if token else {}
		self.headers = headers or {}
		self._body = body

	def get_data(self):
		return self._body


class TestWebhookIngress(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.cfg = registry.by_channel("telephony")
		if not frappe.db.exists("CRM Telephony Account", ACCOUNT):
			frappe.get_doc(
				{
					"doctype": "CRM Telephony Account",
					"account_name": ACCOUNT,
					"provider": "Acefone", "api_token": "ingress-test-token", "caller_id": "919000100002",
					"enabled": 1,
				}
			).insert(ignore_permissions=True)
		cls._original_header_reader = frappe.get_request_header
		frappe.get_request_header = lambda name, default=None: (
			getattr(frappe.local, "request", None) and frappe.local.request.headers.get(name)
		) or default

	@classmethod
	def tearDownClass(cls):
		frappe.get_request_header = cls._original_header_reader
		super().tearDownClass()

	def setUp(self):
		self._configure(webhook_token=TOKEN, webhook_auth_mode="Token", webhook_enforce_ip=0,
		                webhook_token_previous="", webhook_ip_allowlist="")

	def _configure(self, **values):
		doc = frappe.get_doc("CRM Telephony Account", ACCOUNT)
		doc.update(values)
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		frappe.clear_cache(doctype="CRM Telephony Account")
		return doc

	def _request(self, token=TOKEN, body=b"{}", headers=None, ip="203.0.113.10"):
		frappe.local.request = _Request(token, body, headers)
		frappe.local.request_ip = ip
		frappe.local.form_dict = frappe._dict({"token": token} if token else {})

	def test_the_digest_is_derived_on_save_and_never_entered(self):
		"""A Password field cannot be indexed, so the digest is what makes auth a single read."""
		stored = frappe.db.get_value("CRM Telephony Account", ACCOUNT, "webhook_token_hash")
		self.assertEqual(stored, hashlib.sha256(TOKEN.encode()).hexdigest())

	def test_a_valid_token_authenticates(self):
		self._request()
		self.assertEqual(ingress.verify("telephony"), ACCOUNT)

	def test_an_unknown_token_is_denied_at_the_gate(self):
		"""Denied before an adapter is reached, and before the payload is even logged."""
		self._request(token="_test-not-a-real-token")
		with self.assertRaises(frappe.PermissionError):
			ingress.verify("telephony")

	def test_a_missing_token_is_denied(self):
		self._request(token=None)
		with self.assertRaises(frappe.PermissionError):
			ingress.verify("telephony")

	def test_an_unknown_token_costs_no_decryption(self):
		"""The point of the digest. A rotating-token flood must not amplify into N decryptions."""
		self._request(token="_test-unknown")
		calls = []
		original = frappe.get_cached_doc
		frappe.get_cached_doc = lambda *a, **kw: (calls.append(a), original(*a, **kw))[1]
		try:
			with self.assertRaises(frappe.PermissionError):
				ingress.verify("telephony")
		finally:
			frappe.get_cached_doc = original
		self.assertEqual(calls, [], "an unknown token must not load any account")

	def test_both_tokens_authenticate_during_a_rotation(self):
		"""Providers retry very few times, so a call rejected mid-rotation is lost for good."""
		self._configure(webhook_token="_test-rotated-token", webhook_token_previous=TOKEN)

		self._request(token=TOKEN)
		self.assertEqual(ingress.verify("telephony"), ACCOUNT)

		self._request(token="_test-rotated-token")
		self.assertEqual(ingress.verify("telephony"), ACCOUNT)

	def test_the_ip_allowlist_is_off_until_an_operator_turns_it_on(self):
		"""Not every provider publishes egress addresses, so enforcement cannot be the default."""
		self._request(ip="198.51.100.99")
		self.assertEqual(ingress.verify("telephony"), ACCOUNT)

	def test_an_enforced_allowlist_rejects_a_caller_outside_it(self):
		self._configure(webhook_enforce_ip=1, webhook_ip_allowlist="203.0.113.0/24\n198.51.100.7 # a host")

		self._request(ip="203.0.113.55")
		self.assertEqual(ingress.verify("telephony"), ACCOUNT)

		self._request(ip="198.51.100.7")
		self.assertEqual(ingress.verify("telephony"), ACCOUNT)

		self._request(ip="9.9.9.9")
		with self.assertRaises(frappe.PermissionError):
			ingress.verify("telephony")

	def test_enforcing_an_empty_allowlist_is_refused_at_save(self):
		"""A config that would silently reject every call must not be savable."""
		with self.assertRaises(frappe.ValidationError):
			self._configure(webhook_enforce_ip=1, webhook_ip_allowlist="")
		frappe.db.rollback()

	def test_a_valid_signature_passes_and_a_forged_one_does_not(self):
		self._configure(
			webhook_auth_mode="Token + HMAC",
			webhook_hmac_secret=SECRET,
			webhook_hmac_header="X-Signature",
			webhook_hmac_algo="sha256",
			webhook_hmac_encoding="Hex",
			webhook_hmac_prefix="sha256=",
		)
		signature = "sha256=" + hmac.new(SECRET.encode(), BODY, "sha256").hexdigest()

		self._request(body=BODY, headers={"X-Signature": signature})
		self.assertEqual(ingress.verify("telephony"), ACCOUNT)

		self._request(body=BODY, headers={"X-Signature": "sha256=deadbeef"})
		with self.assertRaises(frappe.PermissionError):
			ingress.verify("telephony")

	def test_a_tampered_body_fails_its_own_signature(self):
		"""Signed over the RAW body. A parsed dict reorders keys and would never reproduce this."""
		self._configure(
			webhook_auth_mode="Token + HMAC",
			webhook_hmac_secret=SECRET,
			webhook_hmac_header="X-Signature",
			webhook_hmac_prefix="sha256=",
		)
		signature = "sha256=" + hmac.new(SECRET.encode(), BODY, "sha256").hexdigest()

		self._request(body=b'{"call_id":"tampered"}', headers={"X-Signature": signature})
		with self.assertRaises(frappe.PermissionError):
			ingress.verify("telephony")

	def test_a_base64_signature_is_honoured(self):
		"""Providers differ on encoding, so it is declared per account rather than assumed."""
		self._configure(
			webhook_auth_mode="Token + HMAC",
			webhook_hmac_secret=SECRET,
			webhook_hmac_header="X-Signature",
			webhook_hmac_encoding="Base64",
			webhook_hmac_prefix="",
		)
		signature = base64.b64encode(hmac.new(SECRET.encode(), BODY, "sha256").digest()).decode()

		self._request(body=BODY, headers={"X-Signature": signature})
		self.assertEqual(ingress.verify("telephony"), ACCOUNT)

	def test_enabling_hmac_without_a_secret_is_refused_at_save(self):
		with self.assertRaises(frappe.ValidationError):
			self._configure(webhook_auth_mode="Token + HMAC", webhook_hmac_secret="", webhook_hmac_header="")
		frappe.db.rollback()

	def test_two_accounts_sharing_a_token_authenticate_neither(self):
		"""Fail closed on an ambiguous secret. An operator copy-pasting a token across two accounts
		must surface as a rejection, never as a silent guess that cross-attributes one tenant's
		inbound traffic to the other."""
		twin = "_TestIngressTwin"
		if not frappe.db.exists("CRM Telephony Account", twin):
			frappe.get_doc(
				{"doctype": "CRM Telephony Account", "account_name": twin, "provider": "Acefone", "api_token": "ingress-test-token", "caller_id": "919000100002", "enabled": 1}
			).insert(ignore_permissions=True)
		doc = frappe.get_doc("CRM Telephony Account", twin)
		doc.webhook_token = TOKEN          # the same secret as ACCOUNT
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		frappe.clear_cache(doctype="CRM Telephony Account")

		try:
			self._request(token=TOKEN)
			with self.assertRaises(frappe.PermissionError):
				ingress.verify("telephony")
		finally:
			frappe.delete_doc("CRM Telephony Account", twin, force=True, ignore_permissions=True)
			frappe.db.commit()

	def test_a_disabled_account_authenticates_nothing(self):
		"""Turning an integration off must stop its traffic, not merely hide it from a list."""
		self._configure(enabled=0)
		try:
			self._request()
			with self.assertRaises(frappe.PermissionError):
				ingress.verify("telephony")
		finally:
			self._configure(enabled=1)

	def test_hmac_holds_against_a_real_request(self):
		"""Driven through a REAL werkzeug request, not a stub.

		The two things that could actually break here are the ones a stub hides: whether the raw body
		survives Frappe parsing it into form_dict, and whether the signature header is found regardless
		of the case the provider sent it in. Both are exercised only by a real request object.
		"""
		self._configure(
			webhook_auth_mode="Token + HMAC",
			webhook_hmac_secret=SECRET,
			webhook_hmac_header="X-Signature",
			webhook_hmac_encoding="Hex",
			webhook_hmac_prefix="",
		)
		signature = hmac.new(SECRET.encode(), BODY, "sha256").hexdigest()

		builder = EnvironBuilder(
			method="POST",
			query_string={"token": TOKEN},
			data=BODY,
			content_type="application/json",
			# Deliberately lower-cased: HTTP headers are case-insensitive and a provider may send any case.
			headers={"x-signature": signature},
		)
		request = Request(builder.get_environ())

		frappe.local.request = request
		frappe.local.request_ip = "203.0.113.10"
		# Frappe reads the body into form_dict before a handler runs. Reproduced here, because if that
		# consumed the stream the signature could never be recomputed.
		frappe.local.form_dict = frappe._dict(frappe.parse_json(request.get_data(as_text=True)))
		frappe.local.form_dict.token = TOKEN
		_ = request.form

		self.assertEqual(request.get_data(), BODY, "the raw body must survive form parsing")
		self.assertEqual(ingress.verify("telephony"), ACCOUNT)

	def test_every_channel_declares_an_ingress_prefix(self):
		"""The one knob a new channel sets. Without it the whole auth surface silently misses."""
		for name, cfg in registry.CHANNELS.items():
			self.assertIn("ingress_prefix", cfg, name)
			self.assertEqual(
				ingress.field(cfg, "token"), cfg["token_field"],
				f"{name}: the prefix must derive the same token field the registry declares",
			)
			self.assertTrue(
				cfg.get("active_filter"),
				f"{name}: must declare what makes an account live, or a disabled one still authenticates",
			)
			self.assertTrue(
				cfg.get("adapters"),
				f"{name}: a channel with no adapter can authenticate a caller and then serve nobody",
			)
			self.assertTrue(
				cfg.get("provider_field"),
				f"{name}: the account row is what names the vendor — without the field nothing can resolve one",
			)
