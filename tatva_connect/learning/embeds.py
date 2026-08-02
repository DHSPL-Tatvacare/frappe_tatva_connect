# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Microsoft Office share links are served to the reader as embeds, and never stored as one.

READ TIME, not write time. The lesson editor cannot represent an `embed` block: it round-trips a
lesson through its own model, and a block type it does not know is DROPPED and the loss persisted.
Measured on a seeded lesson — `Version` recorded 19→20 blocks as an embed was written, then 20→19
thirty-four seconds later when the editor next saved, and two more went the same way. Anything we
put in the field is therefore destroyed by the author's next save. So nothing is put there: the
stored lesson keeps the bare URL the author typed, which the editor round-trips perfectly, and
`get_lesson` resolves it on the way out.

This is upstream's own pattern in upstream's own method — `serve_resource` says of it, "get_lesson
rewrites embedded URLs to this endpoint for every user". No fork of frappe/lms, no frontend build.

Upstream registers Google services only — docsPublic / sheetsPublic / slidesPublic in
lms/frontend/src/utils/index.js. The Microsoft equivalent is the SAME share link asked for in its
embeddable form. One document, two actions: `default` is the interactive UI, which redirects an
anonymous reader to sign-in and refuses to be framed; `embedview` is the read-only viewer, which an
anyone-with-the-link share resolves without credentials and which sets no framing header at all.
Only the second is embeddable, so the action is decided here and never taken from the author.

The document must still be shared anyone-with-the-link; a tenant-restricted link renders a sign-in
wall inside the iframe, which is operator config and not something code can fix.

`service` MUST name a service the client already registers: render() destructures `html` from
`services[data.service]`, and an unknown name throws on undefined and blanks the whole lesson. Two
upstream names are therefore reused, for their iframe shell and nothing else — `data.embed` is what
actually loads. That coupling is the price of not forking, and it is two string literals wide.
"""

import json
import re
from html import unescape
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import frappe

from tatva_connect import automation

TOGGLE = "Learning::Course Lesson::office-embeds"

# The two block-JSON fields `get_lesson` returns; instructor_content is already blanked for students.
_CONTENT_FIELDS = ("content", "instructor_content")

# The block types that carry authored prose; the editor writes `markdown`, the publisher writes `paragraph`.
_PROSE_TYPES = ("paragraph", "markdown")

# Host-anchored, https-only, and never matched on path — SharePoint paths are tenant-shaped. The
# scheme bound is load-bearing: `data.embed` reaches the client as an iframe src, so a `javascript:`
# or `data:` candidate must never be claimable here.
_OFFICE_HOST = re.compile(
	r"^https://(?:[a-z0-9][a-z0-9-]*\.sharepoint\.com|onedrive\.live\.com|1drv\.ms)/\S+$",
	re.IGNORECASE,
)

# The only action that renders anonymously and may be framed; set, never appended, so a pasted one cannot win.
_EMBED_ACTION = "embedview"

# A paragraph that is one anchor and nothing else; the href still faces _OFFICE_HOST like any other candidate.
_ANCHOR = re.compile(r"^<a\s[^>]*?href=[\"'](?P<href>[^\"']+)[\"'][^>]*>.*</a>$", re.IGNORECASE | re.DOTALL)

# PowerPoint wants the shorter shell; SharePoint types a share link with /:p:/, a direct path ends .ppt(x).
_IS_SLIDES = re.compile(r"/:p:/|\.pptx?(?:$|[?#])", re.IGNORECASE)

# Upstream service names, borrowed for their iframe shell only (40rem document, 30rem slide).
_SERVICE_DOCUMENT = "docsPublic"
_SERVICE_SLIDES = "slidesPublic"


@frappe.whitelist(allow_guest=True)  # guest-ok: mirrors the native allow_guest; the native fn keeps its own guest gate, access resolution and instructor-field blanking, and this only rewrites what it already returned
def get_lesson(course: str, chapter: int, lesson: int) -> dict:
	"""The native lesson, with every Microsoft share link in its body resolved into an embed.

	Delegates first and rewrites second, so every access decision stays where upstream makes it — a
	caller who may not read this lesson gets upstream's refusal, unchanged, with nothing added to it.
	"""
	from lms.lms.utils import get_lesson as _native
	from lms.lms.utils import guest_access_allowed

	# Fail closed at our own door too, on the operator's own switch, so this is never the softer way in.
	if not guest_access_allowed():
		return {}

	details = _native(course, chapter, lesson)
	if not automation.is_enabled(TOGGLE):
		return details

	for fieldname in _CONTENT_FIELDS:
		rewritten = rewritten_content(details.get(fieldname))
		if rewritten is not None:
			details[fieldname] = rewritten
	return details


def rewritten_content(raw):
	"""The lesson body with every claimable block resolved, or None when nothing changed.

	None rather than the input, so a body with nothing to claim is handed on exactly as stored — never
	re-serialised, so the reader is never served a JSON string no one authored.
	"""
	if not raw:
		return None

	try:
		payload = json.loads(raw)
	except (TypeError, ValueError):
		# A lesson body that is not EditorJS JSON is not ours to interpret. Leave it exactly as typed.
		return None

	blocks = payload.get("blocks") if isinstance(payload, dict) else None
	if not isinstance(blocks, list):
		return None

	claimed = [_claim(block) for block in blocks if isinstance(block, dict)]
	return json.dumps(payload) if any(claimed) else None


def _claim(block):
	"""Rewrite one block in place into a resolved embed. True when it was actually claimed."""
	url = _office_url(block)
	if not url:
		return False

	caption = block["data"].get("caption") or ""
	block["type"] = "embed"
	block["data"] = {
		"service": _SERVICE_SLIDES if _IS_SLIDES.search(url) else _SERVICE_DOCUMENT,
		"source": url,
		"embed": _embed_url(url),
		"caption": caption,
	}
	return True


def _embed_url(url):
	"""The same share link, asked for in its embeddable form — parsed and rebuilt, never concatenated."""
	parts = urlsplit(url)
	query = {k: v for k, v in parse_qsl(parts.query) if k.lower() != "action"} | {"action": _EMBED_ACTION}
	return urlunsplit(parts._replace(query=urlencode(query, quote_via=quote)))


def _paragraph_url(text):
	"""What a paragraph is offering as a whole — itself, or the href of the one link it consists of.

	A share button copies rich text, so the paragraph holds `<a href=…>Deck.pptx</a>` and never the bare
	URL. Nothing else may accompany it: a link inside a sentence is prose the author wrote.
	"""
	text = (text or "").strip()
	sole_anchor = _ANCHOR.match(text) if text.count("<a ") == 1 else None
	return unescape(sole_anchor.group("href")) if sole_anchor else text


def _office_url(block):
	"""The Microsoft share link this block is really carrying, or None.

	Two shapes reach the server, because LMS has two ways of turning a typed URL into a block. A URL
	followed by Enter is converted to an embed block carrying only `source` (markdownParser
	`_convertBlock`) — no service matched it client-side, so today it renders as an empty div. A URL
	left as prose stays a paragraph, bare or as the single anchor a rich-text paste leaves. All are
	claimed.

	A block whose `service` is already set is never touched, which is what makes this idempotent: the
	second save of an already-resolved lesson finds nothing to claim and leaves the field alone.
	"""
	data = block.get("data")
	if not isinstance(data, dict):
		return None

	if block.get("type") == "embed":
		candidate = "" if data.get("service") else data.get("source")
	elif block.get("type") in _PROSE_TYPES:
		candidate = _paragraph_url(data.get("text"))
	else:
		return None

	candidate = (candidate or "").strip()
	return candidate if _OFFICE_HOST.match(candidate) else None
