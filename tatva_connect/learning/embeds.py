# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Microsoft Office share links become lesson embeds, decided on the server.

LMS resolves an embed exactly ONCE, in the author's browser, at paste time: `@editorjs/embed`
runs the service regex in `onPaste` and PERSISTS the resolved iframe src into the block as
`data.embed`. Render never re-runs the regex — it reads `data.embed` and looks `data.service` up
only to pick the iframe shell (embed.mjs:241-247). So "which URL becomes which iframe" is a
WRITE-time decision, and write time is ours: no fork of frappe/lms, no frontend build, no rebuilt
`public/frontend/assets` to carry across upgrades.

Upstream registers Google services only — docsPublic / sheetsPublic / slidesPublic in
lms/frontend/src/utils/index.js. This writes the Microsoft equivalent through the Office Online
viewer, which renders doc/docx/xls/xlsx/ppt/pptx read-only from a link Microsoft's servers can
themselves fetch (so the document must be shared anyone-with-the-link; a tenant-restricted link
renders a sign-in wall inside the iframe, which is operator config and not something code can fix).

`service` MUST name a service the client already registers: render() destructures `html` from
`services[data.service]`, and an unknown name throws on undefined and blanks the whole lesson. Two
upstream names are therefore reused, for their iframe shell and nothing else — `data.embed` is what
actually loads. That coupling is the price of not forking, and it is two string literals wide.
"""

import json
import re
from urllib.parse import quote

from tatva_connect import automation

TOGGLE = "Learning::Course Lesson::office-embeds"

# Both EditorJS content fields on a Course Lesson hold the same block JSON.
_CONTENT_FIELDS = ("content", "instructor_content")

# Host-anchored, https-only, and never matched on path — SharePoint paths are tenant-shaped. The
# scheme bound is load-bearing: `data.embed` reaches the client as an iframe src, so a `javascript:`
# or `data:` candidate must never be claimable here.
_OFFICE_HOST = re.compile(
	r"^https://(?:[a-z0-9][a-z0-9-]*\.sharepoint\.com|onedrive\.live\.com|1drv\.ms)/\S+$",
	re.IGNORECASE,
)

_VIEWER = "https://view.officeapps.live.com/op/embed.aspx?src={src}"

# PowerPoint wants the shorter shell; SharePoint types a share link with /:p:/, a direct path ends .ppt(x).
_IS_SLIDES = re.compile(r"/:p:/|\.pptx?(?:$|[?#])", re.IGNORECASE)

# Upstream service names, borrowed for their iframe shell only (40rem document, 30rem slide).
_SERVICE_DOCUMENT = "docsPublic"
_SERVICE_SLIDES = "slidesPublic"


def rewrite_office_links(doc, method=None):
	"""Claim every unresolved Microsoft share link in the lesson body as a rendered embed.

	Rides `before_save`, so it covers both write paths the lesson editor uses — the SPA saves through
	`frappe.client.insert` and `frappe.client.set_value` (LessonForm.vue:195,209) and both end in
	`doc.save()`. Nothing here validates, so it sits after validate rather than before it.
	"""
	if not automation.is_enabled(TOGGLE):
		return

	for fieldname in _CONTENT_FIELDS:
		rewritten = rewritten_content(doc.get(fieldname))
		if rewritten is not None:
			doc.set(fieldname, rewritten)


def rewritten_content(raw):
	"""The lesson body with every claimable block resolved, or None when nothing changed.

	None rather than the input, so an untouched lesson is never rewritten — re-serialising JSON that
	no one asked us to change would dirty the field on every save and churn the version history.
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
		"embed": _VIEWER.format(src=quote(url, safe="")),
		"caption": caption,
	}
	return True


def _office_url(block):
	"""The Microsoft share link this block is really carrying, or None.

	Two shapes reach the server, because LMS has two ways of turning a typed URL into a block. A URL
	followed by Enter is converted to an embed block carrying only `source` (markdownParser
	`_convertBlock`) — no service matched it client-side, so today it renders as an empty div. A URL
	left as prose stays a paragraph. Both are claimed.

	A block whose `service` is already set is never touched, which is what makes this idempotent: the
	second save of an already-resolved lesson finds nothing to claim and leaves the field alone.
	"""
	data = block.get("data")
	if not isinstance(data, dict):
		return None

	if block.get("type") == "embed":
		candidate = "" if data.get("service") else data.get("source")
	elif block.get("type") == "paragraph":
		# Whole-value match only — a URL mentioned mid-sentence is prose the author wrote, not an embed.
		candidate = data.get("text")
	else:
		return None

	candidate = (candidate or "").strip()
	return candidate if _OFFICE_HOST.match(candidate) else None
