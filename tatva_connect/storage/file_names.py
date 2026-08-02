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
