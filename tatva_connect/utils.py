"""Shared low-level utilities for tatva_connect."""
import ipaddress
import socket
from typing import NoReturn
from urllib.parse import urlparse

import frappe

# ipaddress flags that mark a non-public (internal) address — fail-closed against SSRF.
_BLOCKED_FLAGS = (
	"is_private",
	"is_loopback",
	"is_link_local",
	"is_reserved",
	"is_multicast",
	"is_unspecified",
)


def assert_safe_public_url(url: str, allowed_hosts: "str | list | None" = None) -> None:
	"""Block an outbound fetch whose host resolves to an internal / non-public address (SSRF).

	Fail-closed: raises frappe.ValidationError on anything unsafe (bad scheme/host, off-domain,
	unresolvable, or resolving to a private/loopback/link-local/reserved/multicast/unspecified
	IP — e.g. 169.254.169.254 cloud metadata or a 10.x host). Does NOT defend against
	DNS-rebinding TOCTOU between this resolve and the later fetch — accepted, out of scope.

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
		if any(getattr(ipaddress.ip_address(ip), flag) for flag in _BLOCKED_FLAGS):
			_block(url, f"resolves to non-public address {ip}")


def _block(url: str, reason: str) -> NoReturn:
	frappe.log_error(title="Blocked unsafe outbound URL", message=f"{reason}: {url}")
	raise frappe.ValidationError(f"Refusing to fetch unsafe URL ({reason}).")
