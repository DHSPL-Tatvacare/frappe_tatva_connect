"""Inbound webhook authentication — one gate, every provider, configured per account.

Every future integration arrives through here, so the contract is deliberately narrow and the
config is data rather than code. A provider declares an `ingress_prefix` in the registry and gets
the whole surface; nothing new is written per provider.

Three factors, applied in order, each independently switchable on the account:

  TOKEN    always. A shared secret carried in the URL path. Verified in one indexed read.
  HMAC     when the provider signs its payloads. Off by default; Acefone and WATI do not sign.
  IP       when the provider publishes egress ranges. Off by default; not every provider does.

The token is verified by DIGEST, not by scan. The old path read and decrypted every account's token
on every request, which turned one unauthenticated HTTP request into N decryptions and gave an
attacker a cheap CPU amplifier. The digest is a SHA-256 of a high-entropy secret, so it is not itself
a secret and can be indexed. The row is narrowed by digest, then the secret is still compared with
`hmac.compare_digest`, so a wrong token can be neither enumerated nor timed.

Rotation is overlapping by design. Both the current and the previous token authenticate, so rotating
a secret never rejects a live call. This matters because providers give up quickly: Acefone retries a
failed delivery only twice, so a call rejected during a rotation window is a call lost for good.

A rejected request returns 403. A 4xx tells a provider the failure is permanent and stops the
retries, which is correct — a wrong token never becomes right.
"""
import base64
import hashlib
import hmac
import ipaddress

import frappe
from frappe import _
from frappe.utils import cint

from tatva_connect.webhooks import registry

_SUPPORTED_ALGOS = ("sha256", "sha512", "sha1")

# The operator-tunable cap, on each provider's settings Single.
RATE_LIMIT_FIELD = "webhook_rate_limit_per_minute"


def rate_limit_for(settings_doctype: str, fallback: int):
	"""A per-minute cap an operator can tune, as a callable for `frappe.rate_limit`.

	Frappe evaluates the limit per request, so the setting takes effect without a deploy. It is read
	off a Single, which Frappe caches. A blank or zero setting means the built-in default rather than
	"no calls allowed" — a knob left untouched must never lock a provider out.
	"""

	def limit():
		try:
			return cint(frappe.db.get_single_value(settings_doctype, RATE_LIMIT_FIELD)) or fallback
		except Exception:
			# The setting is unreadable (mid-migrate, say). A webhook must not fail on a missing knob.
			return fallback

	return limit


def token_digest(token: str) -> str:
	"""The indexable, non-secret handle for a token."""
	return hashlib.sha256((token or "").strip().encode()).hexdigest()


def field(service_or_cfg, suffix: str) -> str:
	"""The account fieldname a provider uses for one ingress setting."""
	cfg = service_or_cfg if isinstance(service_or_cfg, dict) else registry.by_service(service_or_cfg)
	return f"{(cfg or {}).get('ingress_prefix', '')}webhook_{suffix}"


def verify(service: str) -> str:
	"""Authenticate an inbound request and return the receiving account. Raises on any failure."""
	cfg = registry.by_service(service)
	if not cfg:
		raise frappe.PermissionError(f"No webhook provider registered for {service!r}")

	account = _account_for_token(cfg, request_token())
	if not account:
		raise frappe.PermissionError(f"Invalid {service} webhook token")

	doc = frappe.get_cached_doc(cfg["account_doctype"], account)
	_assert_ip_allowed(doc, cfg, service)
	_assert_signature(doc, cfg, service)
	return account


def request_token():
	"""The secret carried in the URL. nginx rewrites the path segment to a `token` query param."""
	token = frappe.request.args.get("token") if frappe.request else None
	return token or frappe.form_dict.get("token")


def _account_for_token(cfg, token):
	"""The one account a token authenticates, or None. The previous token is accepted for rotation."""
	token = (token or "").strip()
	if not token:
		return None
	pairs = (
		(field(cfg, "token"), field(cfg, "token_hash")),
		(field(cfg, "token_previous"), field(cfg, "token_previous_hash")),
	)
	for token_field, hash_field in pairs:
		account = _match(cfg, token_field, hash_field, token)
		if account:
			return account
	return None


def _match(cfg, token_field, hash_field, token):
	"""Narrow by indexed digest, then confirm the secret in constant time. Fail-closed on ambiguity.

	A disabled account is not a candidate: turning an integration off must stop its traffic, not merely
	hide it from a list.
	"""
	doctype = cfg["account_doctype"]
	filters = {hash_field: token_digest(token), **cfg.get("active_filter", {})}
	names = frappe.get_all(doctype, filters=filters, pluck="name", limit=2)
	if len(names) != 1:
		# Two accounts sharing a token is a misconfiguration, not a routing choice.
		return None
	stored = frappe.get_cached_doc(doctype, names[0]).get_password(token_field, raise_exception=False)
	if stored and hmac.compare_digest(str(stored), token):
		return names[0]
	return None


def _assert_ip_allowed(doc, cfg, service):
	"""Reject a caller outside the account's allowlist. Enforced only when the operator turns it on."""
	if not doc.get(field(cfg, "enforce_ip")):
		return

	allowed = _networks(doc.get(field(cfg, "ip_allowlist")))
	caller = frappe.local.request_ip
	try:
		address = ipaddress.ip_address(caller)
	except ValueError:
		raise frappe.PermissionError(f"{service} webhook: unreadable caller address")

	if not any(address in network for network in allowed):
		frappe.logger("webhooks").warning(f"{service} webhook: {caller} is not in the account allowlist")
		raise frappe.PermissionError(f"{service} webhook: caller not allowed")


def _networks(allowlist):
	"""Parse an operator's allowlist. One address or CIDR per line; unreadable entries are dropped."""
	networks = []
	for line in (allowlist or "").splitlines():
		entry = line.split("#", 1)[0].strip()
		if not entry:
			continue
		try:
			networks.append(ipaddress.ip_network(entry, strict=False))
		except ValueError:
			frappe.logger("webhooks").warning(f"webhook allowlist: {entry!r} is not an address or CIDR")
	return networks


def _assert_signature(doc, cfg, service):
	"""Verify the provider's HMAC over the raw body. Enforced only when the account declares it."""
	if (doc.get(field(cfg, "auth_mode")) or "Token") != "Token + HMAC":
		return

	secret = doc.get_password(field(cfg, "hmac_secret"), raise_exception=False)
	header = (doc.get(field(cfg, "hmac_header")) or "").strip()
	if not (secret and header):
		raise frappe.PermissionError(f"{service} webhook: HMAC is enabled but not configured")

	algo = (doc.get(field(cfg, "hmac_algo")) or "sha256").strip().casefold()
	if algo not in _SUPPORTED_ALGOS:
		raise frappe.PermissionError(f"{service} webhook: unsupported HMAC algorithm {algo!r}")

	sent = (frappe.get_request_header(header) or "").strip()
	prefix = (doc.get(field(cfg, "hmac_prefix")) or "").strip()
	if prefix and sent.startswith(prefix):
		sent = sent[len(prefix):]

	# Signed over the RAW body. A parsed dict reorders keys and would never reproduce the signature.
	body = frappe.request.get_data() if frappe.request else b""
	digest = hmac.new(str(secret).encode(), body, algo)
	encoding = (doc.get(field(cfg, "hmac_encoding")) or "Hex").strip()
	expected = (
		base64.b64encode(digest.digest()).decode() if encoding == "Base64" else digest.hexdigest()
	)

	if not hmac.compare_digest(expected, sent):
		frappe.logger("webhooks").warning(f"{service} webhook: HMAC signature mismatch")
		raise frappe.PermissionError(f"{service} webhook: bad signature")


def sync_token_digests(doc, method=None):
	"""Keep an account's token digests in step with its tokens. Wired on both account doctypes.

	A Password field cannot be indexed, so the digest is what makes authentication a single read. It
	is derived here rather than entered, and it is never a secret.
	"""
	cfg = registry.by_account_doctype(doc.doctype)
	if not cfg:
		return
	for suffix in ("token", "token_previous"):
		token = doc.get_password(field(cfg, suffix), raise_exception=False)
		digest = token_digest(token) if token else None
		if doc.get(field(cfg, f"{suffix}_hash")) != digest:
			doc.db_set(field(cfg, f"{suffix}_hash"), digest, update_modified=False)


def assert_ingress_config(doc, method=None):
	"""Refuse a configuration that would silently reject every call. Runs at validate."""
	cfg = registry.by_account_doctype(doc.doctype)
	if not cfg:
		return

	if doc.get(field(cfg, "enforce_ip")) and not _networks(doc.get(field(cfg, "ip_allowlist"))):
		frappe.throw(
			_("IP enforcement is on but the allowlist is empty, which would reject every call. "
			  "Add the provider's egress addresses, or turn enforcement off."),
			title=_("Incomplete IP allowlist"),
		)

	if (doc.get(field(cfg, "auth_mode")) or "Token") == "Token + HMAC":
		if not (doc.get_password(field(cfg, "hmac_secret"), raise_exception=False)
		        and (doc.get(field(cfg, "hmac_header")) or "").strip()):
			frappe.throw(
				_("HMAC verification is on but the signing secret or header name is missing, which "
				  "would reject every call."),
				title=_("Incomplete HMAC configuration"),
			)
