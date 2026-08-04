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

A caller may pass its own ceiling, but only to go LOWER for a reason it can name. This module does not
fetch: the URL, the auth, the redirect policy and what a 404 means stay with the caller, because those are
what differ between providers.
"""


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
