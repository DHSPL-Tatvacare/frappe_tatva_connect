# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""What a lesson body must look like AFTER the server has claimed a Microsoft share link.

These assert the stored block, not that a function was called, because the stored block is the whole
mechanism: `@editorjs/embed` render() reads `data.embed` for the iframe src and `data.service` for
the shell, and never re-runs a regex. If what is written here is wrong, the lesson renders blank or
throws on undefined — and nothing else in the stack would notice.
"""

import json
import unittest
from urllib.parse import parse_qsl, urlsplit

from tatva_connect.learning.embeds import (
	_SERVICE_DOCUMENT,
	_SERVICE_SLIDES,
	rewritten_content,
)

_SHAREPOINT_DOC = "https://contoso.sharepoint.com/:w:/g/personal/x/EaBc123?e=9kLmNo"
_SHAREPOINT_DECK = "https://contoso.sharepoint.com/:p:/g/personal/x/EaBc123?e=9kLmNo"


def _body(*blocks):
	return json.dumps({"time": 1, "blocks": list(blocks), "version": "2.29.0"})


def _blocks(raw):
	return json.loads(raw)["blocks"]


class TestOfficeEmbeds(unittest.TestCase):
	def test_unresolved_embed_block_becomes_a_loadable_iframe(self):
		"""The shape LMS leaves behind when no client service matched the pasted URL."""
		out = rewritten_content(_body({"type": "embed", "data": {"source": _SHAREPOINT_DOC}}))

		block = _blocks(out)[0]
		self.assertEqual(block["type"], "embed")
		self.assertEqual(block["data"]["service"], _SERVICE_DOCUMENT)
		self.assertEqual(block["data"]["source"], _SHAREPOINT_DOC)
		self.assertEqual(
			block["data"]["embed"],
			"https://contoso.sharepoint.com/:w:/g/personal/x/EaBc123?e=9kLmNo&action=embedview",
		)

	def test_the_embed_never_leaves_the_host_the_author_shared_from(self):
		"""The iframe src stays on the allowlisted host — no third-party viewer ever sees the link."""
		out = rewritten_content(_body({"type": "embed", "data": {"source": _SHAREPOINT_DOC}}))

		self.assertEqual(
			urlsplit(_blocks(out)[0]["data"]["embed"]).netloc, "contoso.sharepoint.com"
		)

	def test_an_author_supplied_action_cannot_win(self):
		"""`action=default` is the sign-in UI and refuses to be framed; ours replaces it, whatever the casing."""
		for pasted in ("?e=9kLmNo&action=default", "?ACTION=default&e=9kLmNo"):
			with self.subTest(pasted):
				url = f"https://contoso.sharepoint.com/:w:/g/personal/x/EaBc123{pasted}"
				out = rewritten_content(_body({"type": "embed", "data": {"source": url}}))

				query = dict(parse_qsl(urlsplit(_blocks(out)[0]["data"]["embed"]).query))
				self.assertEqual(query, {"e": "9kLmNo", "action": "embedview"})

	def test_a_link_with_no_query_of_its_own_still_builds(self):
		"""The query is rebuilt, not concatenated, so there is no `?` versus `&` to get wrong."""
		url = "https://contoso.sharepoint.com/sites/team/Shared%20Documents/deck.pptx"
		out = rewritten_content(_body({"type": "embed", "data": {"source": url}}))

		self.assertEqual(_blocks(out)[0]["data"]["embed"], f"{url}?action=embedview")

	def test_powerpoint_takes_the_slide_shell_and_word_the_document_shell(self):
		deck = rewritten_content(_body({"type": "embed", "data": {"source": _SHAREPOINT_DECK}}))
		doc = rewritten_content(_body({"type": "embed", "data": {"source": _SHAREPOINT_DOC}}))

		self.assertEqual(_blocks(deck)[0]["data"]["service"], _SERVICE_SLIDES)
		self.assertEqual(_blocks(doc)[0]["data"]["service"], _SERVICE_DOCUMENT)

	def test_a_bare_url_left_as_prose_is_claimed(self):
		out = rewritten_content(_body({"type": "paragraph", "data": {"text": _SHAREPOINT_DOC}}))

		self.assertEqual(_blocks(out)[0]["type"], "embed")

	def test_the_block_type_the_lesson_editor_actually_writes_is_claimed(self):
		"""The editor stores prose as `markdown`; the publisher stores it as `paragraph`. Both are prose."""
		out = rewritten_content(_body({"type": "markdown", "data": {"text": _SHAREPOINT_DOC}}))

		block = _blocks(out)[0]
		self.assertEqual(block["type"], "embed")
		self.assertEqual(block["data"]["service"], _SERVICE_DOCUMENT)
		self.assertIn("action=embedview", block["data"]["embed"])

	def test_a_paragraph_that_is_one_pasted_link_is_claimed(self):
		"""A share button copies rich text, so the paragraph holds an anchor and never the bare URL."""
		text = f'<a href="{_SHAREPOINT_DECK}&amp;x=1">Induction Deck.pptx</a>'
		out = rewritten_content(_body({"type": "paragraph", "data": {"text": text}}))

		block = _blocks(out)[0]
		self.assertEqual(block["type"], "embed")
		self.assertEqual(block["data"]["service"], _SERVICE_SLIDES)
		self.assertIn("action=embedview", block["data"]["embed"])

	def test_a_link_the_author_wrote_into_a_sentence_is_left_alone(self):
		"""Nothing may accompany the link — an anchor mid-sentence is prose, not an embed."""
		text = f'See <a href="{_SHAREPOINT_DOC}">the deck</a> before Monday.'

		self.assertIsNone(rewritten_content(_body({"type": "paragraph", "data": {"text": text}})))

	def test_a_url_inside_a_sentence_is_left_as_prose(self):
		"""Whole-value match only — the author wrote a sentence, not an embed."""
		text = f"The deck lives at {_SHAREPOINT_DOC} if you need it."

		self.assertIsNone(rewritten_content(_body({"type": "paragraph", "data": {"text": text}})))

	def test_an_already_resolved_block_is_left_alone(self):
		"""Idempotence: `service` set means the client already resolved it, or we did on an earlier save."""
		resolved = {
			"type": "embed",
			"data": {"service": "youtube", "source": "https://youtu.be/abc", "embed": "https://youtu.be/abc"},
		}

		self.assertIsNone(rewritten_content(_body(resolved)))

	def test_a_google_link_is_not_claimed(self):
		"""Upstream already registers the Google services; claiming them would be a second brain."""
		google = "https://docs.google.com/document/d/abc123/edit"

		self.assertIsNone(rewritten_content(_body({"type": "embed", "data": {"source": google}})))

	def test_a_non_https_candidate_is_never_claimable(self):
		"""`data.embed` reaches the client as an iframe src — the scheme bound is load-bearing."""
		for hostile in (
			"javascript:alert(1)//contoso.sharepoint.com/x",
			"http://contoso.sharepoint.com/:w:/g/x",
			"https://evil.com/contoso.sharepoint.com/x",
		):
			with self.subTest(hostile):
				self.assertIsNone(
					rewritten_content(_body({"type": "embed", "data": {"source": hostile}}))
				)

	def test_the_caption_the_author_typed_survives(self):
		block = {"type": "embed", "data": {"source": _SHAREPOINT_DOC, "caption": "Induction deck"}}

		self.assertEqual(_blocks(rewritten_content(_body(block)))[0]["data"]["caption"], "Induction deck")

	def test_every_link_in_a_lesson_is_claimed_not_just_the_first(self):
		"""A lesson may carry several documents; claiming stops at none of them."""
		out = rewritten_content(
			_body(
				{"type": "markdown", "data": {"text": _SHAREPOINT_DOC}},
				{"type": "header", "data": {"text": "Then the deck"}},
				{"type": "markdown", "data": {"text": _SHAREPOINT_DECK}},
			)
		)

		types = [b["type"] for b in _blocks(out)]
		self.assertEqual(types, ["embed", "header", "embed"])

	def test_a_block_type_we_do_not_understand_is_never_claimed(self):
		"""Only prose and an unresolved embed are ours; a quiz or an image carrying a URL is not."""
		for kind in ("quiz", "image", "code", "table"):
			with self.subTest(kind):
				block = {"type": kind, "data": {"text": _SHAREPOINT_DOC, "source": _SHAREPOINT_DOC}}

				self.assertIsNone(rewritten_content(_body(block)))

	def test_an_untouched_lesson_is_not_rewritten(self):
		"""None, not the input — re-serialising would dirty the field and churn versions on every save."""
		self.assertIsNone(rewritten_content(_body({"type": "paragraph", "data": {"text": "Hello"}})))
		self.assertIsNone(rewritten_content(""))
		self.assertIsNone(rewritten_content("not json at all"))
