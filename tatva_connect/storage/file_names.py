"""THE display name behind a stored file URL. One derivation, every surface.

A blob key is slugged at mint (`blob_store.new_key`) so it can travel inside a URL: it is a SAFE name,
never the real one. The real one lives on `File.file_name`. Anything that shows a user the name behind
a `file_url` resolves it HERE, so the side panel, the activity form, the timeline and the partner API
cannot disagree about what a file is called.

Visibility is the parent record's decision (constitution I1) — a caller already permitted to read the
document that holds the value is permitted to read what that value is called, so the lookup is a plain
`frappe.qb` read and carries no second permission gate of its own.
"""

import re

import frappe

from tatva_connect.storage import blob_store

_HASH_PREFIX = re.compile(r"^[a-f0-9]{8,}_")


def display_names(file_urls) -> dict:
	"""{file_url -> the name a user should read}. ONE query for the whole batch, never one per URL."""
	urls = [u for u in dict.fromkeys(file_urls or []) if u and isinstance(u, str)]
	if not urls:
		return {}
	f = frappe.qb.DocType("File")
	rows = frappe.qb.from_(f).select(f.file_url, f.file_name).where(f.file_url.isin(urls)).run()
	known = {}
	for url, name in rows:
		# A blob can carry more than one File row (a copy, a ref-counted second owner); first wins.
		if name and url not in known:
			known[url] = name
	return {url: known.get(url) or _from_url(url) for url in urls}


def display_name(file_url) -> str | None:
	"""The single-URL door onto `display_names`. Same brain, one row."""
	return display_names([file_url]).get(file_url)


def _from_url(file_url: str) -> str:
	"""No File row (an external link, a row already deleted): the key's own basename, hash prefix off."""
	tail = blob_store.blob_key_from_url(file_url) or file_url
	base = tail.split("?")[0].split("#")[0].split("/")[-1]
	return _HASH_PREFIX.sub("", base) or file_url


# 255 BYTES: the limit every filesystem enforces per name component (ENAMETOOLONG), and the tighter of
# the two — a 255-byte utf-8 string is at most 255 characters, so it always fits varchar(255) as well.
FILE_NAME_LIMIT = 255


def _head(text: str, budget: int) -> str:
	"""The longest PREFIX whose utf-8 encoding fits `budget` bytes, never splitting a character."""
	return text.encode("utf-8")[:max(budget, 0)].decode("utf-8", "ignore")


def _tail(text: str, budget: int) -> str:
	"""The longest SUFFIX whose utf-8 encoding fits `budget` bytes, never splitting a character."""
	if budget <= 0:
		return ""
	return text.encode("utf-8")[-budget:].decode("utf-8", "ignore")


def fit(file_name: str, limit: int = FILE_NAME_LIMIT) -> str:
	"""`file_name` shortened to fit, extension kept, the middle elided. Measured in BYTES.

	Two different limits sit behind this and BYTES is the tighter one. `tabFile.file_name` is
	varchar(255), counted in characters; the filesystem core writes to (file.py:737) allows 255 BYTES per
	name component and raises OSError 36 past it. A Devanagari name is three bytes a character, so 255
	characters is 765 bytes — it clears the column and the filesystem still refuses it, which is a 500
	with nothing stored. Counting bytes satisfies both, because 255 bytes is never more than 255 chars.

	The name is a LABEL: identity is the row's `name` and the blob key, and `blob_store.new_key` slugs its
	own copy with a hash prefix, so neither is affected here. A name too long is trimmed and the file
	lands, rather than kept whole and dropped.

	The middle goes, not the tail: the head says what the document is and the extension says what it is,
	and both survive. Returns the name unchanged when it already fits, which is every real filename.
	"""
	name = (file_name or "").strip()
	if len(name.encode("utf-8")) <= limit:
		return name
	stem, dot, ext = name.rpartition(".")
	ext = dot + ext if dot and len(ext) <= 20 else ""
	if not ext:
		stem = name
	budget = limit - len(ext.encode("utf-8")) - 1
	if budget < 2:
		return _head(name, limit)
	head = (budget + 1) // 2
	return _head(stem, head) + "~" + _tail(stem, budget - head) + ext
