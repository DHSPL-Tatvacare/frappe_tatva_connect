# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""How bytes come across the wire: streamed, and refused past the size THIS SITE already allows.

`response.content` buffers whatever the other end sends, so a large — or hostile — body lands entirely in
memory and holds its worker for the whole timeout, on a queue shared with every other job.

THE CEILING IS NOT OURS TO INVENT. `frappe.core.api.file.get_max_file_size()` is the operator's number
(System Settings, then site config, then 25 MB) and `File.check_max_file_size` already refuses a save past
it. A constant here would be a second decider for one question, and a HIGHER one is worse than none: the
bytes stream in and buffer only for the save to refuse them anyway, once per retry. So the transfer stops
at exactly the number the save would stop at, and an operator raises both by raising one.

A caller may pass its own ceiling, but only to go LOWER for a reason it can name.

WHAT THE URL, THE AUTH AND A 404 MEAN STAY WITH THE CALLER — those genuinely differ between providers.
THE REDIRECT POLICY DOES NOT, and believing it did cost every call recording this app ever took.

Three sites reached for a provider-named URL and each refused redirects outright, two of them under the
same comment word for word: *"assert_safe_public_url vetted THIS host only; a 3xx could bounce to an
internal target."* The comment named the real limit and settled for the blunt answer. But a signed,
short-lived 302 to object storage is HOW a recording is served — Bolna via S3, and Acefone, Twilio and
Exotel the same — so refusing the hop reads the empty body of a 302 and calls it "no bytes". It fails
every attempt, for every call, for ever, and silently, because a failed fetch is deliberately a gap and
not a failed job.

`fetch_capped` is the one answer: FOLLOW the hop and vet every one of them. That is what
`assert_safe_public_url` could always have supported — the loop around it was simply never written.
"""
from urllib.parse import urljoin, urlparse


def site_ceiling() -> int:
	"""The operator's own maximum file size, in bytes. Read per call, so raising it needs no restart."""
	from frappe.core.api.file import get_max_file_size

	return get_max_file_size()


def read_capped(response, max_bytes: int | None = None, *, chunk: int = 256 * 1024) -> bytes:
	"""The body of an already-streamed response, or ValueError once it passes the ceiling.

	The declared length is judged FIRST so an oversized body is refused before a byte is transferred, and
	the running total is judged again while reading because Content-Length is the sender's claim, not a
	guarantee — a chunked response carries no length at all.
	"""
	ceiling = site_ceiling() if max_bytes is None else max_bytes
	declared = int(response.headers.get("Content-Length") or 0)
	if declared > ceiling:
		raise ValueError(f"the producer declared {declared} bytes, over this site's {ceiling} byte ceiling")
	chunks, total = [], 0
	for part in response.iter_content(chunk):
		total += len(part)
		if total > ceiling:
			raise ValueError(f"the download passed this site's {ceiling} byte ceiling and was stopped")
		chunks.append(part)
	return b"".join(chunks)


# A redirect chain long enough to need this many hops is a loop or a misconfiguration, not a delivery.
MAX_HOPS = 5


def fetch_capped(url, *, timeout, headers=None, allowed_hosts=None, max_bytes=None,
                 chunk: int = 256 * 1024):
	"""GET a URL this app did not author, following redirects safely. -> `(bytes, content_type)`.

	EVERY HOP IS VETTED, not just the first — which is the whole reason this exists and the one thing a
	caller cannot get by passing `allow_redirects=True`. `assert_safe_public_url` re-runs on each
	destination, so a chain that launders through a permitted host into `169.254.169.254` is refused at
	the hop that turns bad, and its refusal names which one.

	THE AUTHORIZATION HEADER IS DROPPED THE MOMENT THE HOST CHANGES. A recording URL is signed by its
	provider and redirects to object storage on a different host; carrying the provider's bearer token
	across would hand our credential to whoever the redirect names. `requests` does this for you and
	following hops by hand means doing it by hand — which is why this is one function and not a policy
	each caller re-implements.

	Streaming and the size ceiling are `read_capped`'s, unchanged. An intermediate response is closed
	before the next hop, so a redirect chain does not hold a connection per hop.
	"""
	import requests  # ALLOWLIST 2026-08-05: raw-byte streaming + manual redirect handling — make_*_request returns processed JSON and cannot express either.

	from tatva_connect.utils import assert_safe_public_url

	target, sending = url, dict(headers or {})
	for _hop in range(MAX_HOPS + 1):
		assert_safe_public_url(target, allowed_hosts)
		response = requests.get(target, headers=sending or None, timeout=timeout,
		                        stream=True, allow_redirects=False)
		if not 300 <= response.status_code < 400:
			response.raise_for_status()
			return read_capped(response, max_bytes, chunk=chunk), response.headers.get("Content-Type")

		location = response.headers.get("Location")
		response.close()  # a streamed 3xx still holds its connection until it is read or closed
		if not location:
			raise ValueError(f"the producer answered {response.status_code} with no destination to follow")
		following = urljoin(target, location)
		if urlparse(following).hostname != urlparse(target).hostname:
			sending = {k: v for k, v in sending.items() if k.lower() != "authorization"}
		target = following

	raise ValueError(f"the producer redirected more than {MAX_HOPS} times without answering")
