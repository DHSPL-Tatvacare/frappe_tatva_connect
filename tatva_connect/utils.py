"""Shared low-level utilities for tatva_connect."""
import ipaddress
import re
import socket
from typing import NoReturn
from urllib.parse import urlparse

import frappe

# A field NAME that carries a secret, whatever the value looks like. This is the general rule: a new
# secret is covered by being named like one, so no per-secret code is ever added here.
_SECRET_NAME = r"[A-Za-z0-9_.\-]*(?:secret|token|password|passwd|api[_-]?key|apikey)[A-Za-z0-9_.\-]*"
# `name=value`, `name: value`, `"name": "value"` and `'name': 'value'` all read the same way.
_KEYED_SECRET = re.compile(rf"({_SECRET_NAME})([\"']?\s*[=:]\s*)([\"']?)([^\s&\"'\\,;)}}\]]*)", re.IGNORECASE)
# A Facebook token travels bare in a header and in Meta's own error text, so shape catches what name cannot.
_SHAPED_SECRET = re.compile(r"EAA[A-Za-z0-9_\-]{10,}")
_VISIBLE = 4
_STARS = "*" * 8


def mask_value(secret: str) -> str:
	"""One secret, masked: a few leading and trailing characters stay readable so an operator can still
	compare and identify the value, and the middle is replaced by a fixed run of asterisks so the length
	is not disclosed either. A value too short to show ends of is masked whole."""
	secret = str(secret or "")
	if len(secret) < 4 * _VISIBLE:
		return _STARS if secret else secret
	return f"{secret[:_VISIBLE]}{_STARS}{secret[-_VISIBLE:]}"


def mask_secrets(text: str, extra=()) -> str:
	"""THE masker: every secret this app can put into a log or a message goes out through here.

	Two rules, both by SHAPE rather than by value, so nothing is decrypted out of the database to write
	a log line. A named field (`client_secret=`, `access_token:`, `"webhook_hmac_secret": "..."`) has its
	value masked whatever that value is, which is what covers a webhook token, an HMAC secret or an API
	key without another line of code. A bare Facebook `EAA...` token is masked on its own shape, because
	it rides an Authorization header and Meta echoes it back inside its own error text under no key at all.

	`extra` masks values the caller already holds, for the in-flight token that is not yet stored anywhere.

	What it cannot catch: a secret written into prose with no name beside it and no recognisable shape.
	It deliberately over-masks instead of under-masking, so a field merely NAMED like a secret loses its
	value even when that value is harmless. A log is cheaper to blind than a credential is to rotate.

	Masking is applied on the way OUT only. It never touches a value that is used."""
	text = str(text or "")
	if not text:
		return text
	for value in extra:
		if value and len(str(value)) >= 6:
			text = text.replace(str(value), mask_value(value))
	text = _KEYED_SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{mask_value(m.group(4))}", text)
	return _SHAPED_SECRET.sub(lambda m: mask_value(m.group(0)), text)


def assert_safe_public_url(url: str, allowed_hosts: "str | list | None" = None) -> None:
	"""Block an outbound fetch whose host resolves to an internal / non-public address (SSRF).

	Fail-closed: raises frappe.ValidationError on anything unsafe (bad scheme/host, off-domain,
	unresolvable, or resolving to a non-global IP). The single `is_global` test covers every
	special-use range in one rule — private/loopback/link-local/reserved/multicast/unspecified
	AND CGNAT (100.64.0.0/10) and any future reserved range — e.g. 169.254.169.254 cloud metadata,
	a 10.x host, or a 100.64.x carrier-NAT address. Does NOT defend against DNS-rebinding TOCTOU
	between this resolve and the later fetch — accepted, out of scope.

	`allowed_hosts` is an optional host or list of hosts (e.g. an operator-configured per-account
	allowlist): when non-empty the URL host must equal or be a sub-domain of one of them; an
	empty/blank allowlist applies no host restriction (the private-IP block still runs).
	"""
	parsed = urlparse(url or "")
	host = parsed.hostname
	if parsed.scheme not in ("http", "https") or not host:
		_block(url, "scheme must be http/https with a host")

	hosts = [allowed_hosts] if isinstance(allowed_hosts, str) else list(allowed_hosts or [])
	if hosts and not any(host == h or host.endswith("." + h) for h in hosts):
		_block(url, f"host is not in the allowlist {hosts}")

	try:
		infos = socket.getaddrinfo(host, None)
	except OSError:
		_block(url, "host does not resolve")

	for info in infos:
		ip = info[4][0]
		if not ipaddress.ip_address(ip).is_global:
			_block(url, f"resolves to non-public address {ip}")


def _block(url: str, reason: str) -> NoReturn:
	frappe.log_error(title="Blocked unsafe outbound URL", message=f"{reason}: {url}")
	raise frappe.ValidationError(f"Refusing to fetch unsafe URL ({reason}).")
