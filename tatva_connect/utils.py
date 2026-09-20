"""Shared low-level utilities for tatva_connect."""
import ipaddress
import re
import socket
import time
from typing import NoReturn
from urllib.parse import urlparse

import frappe
from frappe import _

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


def trim_credentials(doc, *fields: str) -> None:
	"""Strip whitespace a paste carried into a credential field, before it is encrypted and sent.

	A token copied from a terminal or a mail client routinely arrives with a leading or trailing space,
	a Password field renders it as dots so nobody can see it, and the provider rejects a credential the
	operator can see nothing wrong with. A field left untouched holds Frappe's asterisk placeholder,
	which is never a pasted value and is left alone.
	"""
	for field in fields:
		value = doc.get(field)
		if not isinstance(value, str):
			continue
		trimmed = value.strip()
		if not trimmed or trimmed == value or set(trimmed) == {"*"}:
			continue
		doc.set(field, trimmed)


def assert_safe_public_url(url: str, allowed_hosts: "str | list | None" = None,
                            field: "str | None" = None) -> None:
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

	`field` names the request argument the URL arrived in, so an API caller reads it from `error.fields`
	instead of parsing the sentence. A Desk or internal caller names none and the refusal carries none.
	"""
	parsed = urlparse(url or "")
	host = parsed.hostname
	if parsed.scheme not in ("http", "https") or not host:
		_block(url, field,
		       _("its scheme is neither http nor https, or it names no host"),
		       _("Send an absolute URL that begins with https:// and carries a host name."))

	hosts = [allowed_hosts] if isinstance(allowed_hosts, str) else list(allowed_hosts or [])
	if hosts and not any(host == h or host.endswith("." + h) for h in hosts):
		_block(url, field,
		       _("its host `{0}` is not one of the hosts the operator allowed: {1}").format(
			       host, ", ".join(hosts)),
		       _("Send a URL on one of those hosts, or ask the operator to add this host to the allowlist."))

	try:
		infos = socket.getaddrinfo(host, None)
	except OSError:
		_block(url, field,
		       _("its host `{0}` does not resolve").format(host),
		       _("Check the host name, then send a URL on a host that resolves from the public internet."))

	for info in infos:
		ip = info[4][0]
		if not ipaddress.ip_address(ip).is_global:
			_block(url, field,
			       _("its host resolves to {0}, which is not a public internet address").format(ip),
			       _("Send a URL whose host resolves to a public address; an address on the internal "
			         "network is never fetched."))


def _block(url: str, field: "str | None", cause: str, next_step: str) -> NoReturn:
	"""The ONE unsafe-URL refusal: what was found, then the one thing to do about it.

	Routed through `_base.throw_field` — the app's single seam for naming the offending input — rather
	than attaching `fields` here, so this does not become a second copy of that mechanism. The import is
	function-level because utils is the lower layer; `taxonomy/program_mode.py` reaches for it the same way."""
	from tatva_connect.api._base import throw_field

	frappe.log_error(title="Blocked unsafe outbound URL", message=f"{cause}: {url}")
	throw_field(_("This call names an unsafe URL — {0}. {1}").format(cause, next_step),
	            [field] if field else [])


def spend_rate_limit(scope: str, ident: str, limit: int, window: int, message: str,
                     exc=None) -> None:
	"""THE fixed-window redis counter for a NAMED subject — a user, a phone line, a phone number.

	Frappe's own `@rate_limit` keys on the request IP or a form field and nothing else, so it cannot
	express any of those (an office NATs to one address, and a telephony account is not in form_dict).
	It stays the right tool on a guest door, where an IP is the only identity there is; this is the
	right tool everywhere a subject has a name.

	Scope names follow `<area>-rl:<subject>` — `intake-rl:ip`, `telephony-rl:account`, `mcp-rl:user`.

	`exc` overrides the refusal class for a caller whose surface reads only certain statuses: intake
	throws a 417 because frappe's uploader shows the server message on 403/417 alone, and a 429 there
	reaches the visitor as "the file might be corrupted". Everyone else gets the 429 the name implies."""
	key = frappe.cache.make_key(f"{scope}:{ident}")
	if not frappe.cache.get(key):
		frappe.cache.setex(key, window, 0)
	if frappe.cache.incrby(key, 1) > limit:
		frappe.throw(message, exc=exc or frappe.RateLimitExceededError)


def book_drain(key: str, seconds: int) -> bool:
	"""Take the ONE-drain lease for `key`: True for the caller that wins, False for every other.

	`frappe.cache` IS a redis client, so SET NX EX takes it in one atomic move where a read-then-write would
	race. The webhook spine and the workflow drain both lease through here, so "exactly one drain works this
	pile" is one mechanism rather than two. It is a LEASE, not a lock: `seconds` is how long the pile stays
	held once a drain stops speaking for it, so a worker that dies frees the pile in that time rather than
	holding it for as long as a job may run. A drain that works for longer says so with `hold_drain`."""
	return bool(frappe.cache.set(frappe.cache.make_key(key), 1, nx=True, ex=seconds))


def hold_drain(key: str, seconds: int) -> None:
	"""Keep this drain's lease alive while it works — the half that makes a SHORT lease safe for a LONG pass."""
	frappe.cache.expire_key(key, seconds)


def release_drain(key: str) -> None:
	"""Hand the lease back, so the next caller can take it. The other half of `book_drain`."""
	frappe.cache.delete_value(key)


# Share of frappe's own QueueOverloaded ceiling every producer stays below, so a refused job is never its doing.
LANE_BUSY_SHARE = 0.4

# The backlog no producer adds to, whatever that ceiling is: minutes behind is minutes behind at 500 or at 5000.
LANE_BACKLOG_CAP = 200


def lane_depth(queue: str) -> int:
	"""How many jobs are waiting on a lane — RQ's own count, asked, never modelled."""
	from frappe.utils.background_jobs import get_queue

	return get_queue(queue).count


def lane_busy_at() -> int:
	"""Depth at which producers stop feeding a lane: a share of frappe's own refusal point, capped at a real backlog."""
	from frappe.utils.background_jobs import MAX_QUEUED_JOBS

	ceiling = frappe.utils.cint(frappe.conf.max_queued_jobs) or MAX_QUEUED_JOBS
	return min(int(ceiling * LANE_BUSY_SHARE), LANE_BACKLOG_CAP)


def lane_has_room(queue: str) -> bool:
	"""THE back-pressure check: every producer asks it before adding work to a lane."""
	return lane_depth(queue) < lane_busy_at()


ROOM_WAIT_SECONDS = 2  # how long a producer sleeps before asking its lanes again
DELETE_LANE = "default"  # where frappe's delete_doc queues its per-record `delete_dynamic_links` cleanup


def wait_for_room(*queues: str, stop=None) -> None:
	"""Block while any of `queues` is at its busy mark; `stop()` answering True ends the wait early."""
	while not all(lane_has_room(queue) for queue in queues):
		if stop and stop():
			return
		time.sleep(ROOM_WAIT_SECONDS)


def rollback() -> None:
	"""Roll back AND drop frappe's pending webhook queue: a bare rollback leaves it pointing at a flush that is no longer registered."""
	frappe.db.rollback()
	frappe.local._webhook_queue = None


# The database rolled the WHOLE transaction back to break a tie: nothing this request wrote survives, savepoints included.
TRANSACTION_LOST = (frappe.QueryDeadlockError, frappe.QueryTimeoutError)


def retry_on_deadlock(fn, errors=TRANSACTION_LOST, tries=3):
	"""Run fn(); on a transient deadlock roll back in full and retry with backoff; the last one propagates, any other error at once."""
	for attempt in range(tries):
		try:
			return fn()
		except errors:
			rollback()
			if attempt == tries - 1:
				raise
			time.sleep(0.15 * (attempt + 1))


def delete_in_pace(doctype: str, names, delete_one) -> list:
	"""THE way to delete a data-sized set: paced to the cleanup lane, one commit per record, and the names still present returned, never swallowed."""
	failed = []
	for name in names:
		wait_for_room(DELETE_LANE)
		try:
			delete_one(name)
			frappe.db.commit()
		except Exception:
			rollback()  # the caller reports what is left, so a failure here is data, not an exception
			failed.append(name)
	return [name for name in failed if frappe.db.exists(doctype, name)]
