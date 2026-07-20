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

from tatva_connect.learning.embeds import (
	_SERVICE_DOCUMENT,
	_SERVICE_SLIDES,
	rewritten_content,
)

_SHAREPOINT_DOC = "https://tatvacare.sharepoint.com/:w:/g/personal/x/EaBc123?e=9kLmNo"
_SHAREPOINT_DECK = "https://tatvacare.sharepoint.com/:p:/g/personal/x/EaBc123?e=9kLmNo"
_VIEWER = "https://view.officeapps.live.com/op/embed.aspx?src="


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
		self.assertTrue(block["data"]["embed"].startswith(_VIEWER))

	def test_src_is_fully_percent_encoded(self):
		"""The share link carries its own `?` and `=`; unencoded they truncate the viewer's own query."""
		out = rewritten_content(_body({"type": "embed", "data": {"source": _SHAREPOINT_DOC}}))

		src = _blocks(out)[0]["data"]["embed"][len(_VIEWER) :]
		self.assertNotIn("?", src)
		self.assertNotIn("&", src)
		self.assertNotIn("/", src)
		self.assertIn("%3F", src)

	def test_powerpoint_takes_the_slide_shell_and_word_the_document_shell(self):
		deck = rewritten_content(_body({"type": "embed", "data": {"source": _SHAREPOINT_DECK}}))
		doc = rewritten_content(_body({"type": "embed", "data": {"source": _SHAREPOINT_DOC}}))

		self.assertEqual(_blocks(deck)[0]["data"]["service"], _SERVICE_SLIDES)
		self.assertEqual(_blocks(doc)[0]["data"]["service"], _SERVICE_DOCUMENT)

	def test_a_bare_url_left_as_prose_is_claimed(self):
		out = rewritten_content(_body({"type": "paragraph", "data": {"text": _SHAREPOINT_DOC}}))

		self.assertEqual(_blocks(out)[0]["type"], "embed")

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
			"javascript:alert(1)//tatvacare.sharepoint.com/x",
			"http://tatvacare.sharepoint.com/:w:/g/x",
			"https://evil.com/tatvacare.sharepoint.com/x",
		):
			with self.subTest(hostile):
				self.assertIsNone(
					rewritten_content(_body({"type": "embed", "data": {"source": hostile}}))
				)

	def test_the_caption_the_author_typed_survives(self):
		block = {"type": "embed", "data": {"source": _SHAREPOINT_DOC, "caption": "Induction deck"}}

		self.assertEqual(_blocks(rewritten_content(_body(block)))[0]["data"]["caption"], "Induction deck")

	def test_an_untouched_lesson_is_not_rewritten(self):
		"""None, not the input — re-serialising would dirty the field and churn versions on every save."""
		self.assertIsNone(rewritten_content(_body({"type": "paragraph", "data": {"text": "Hello"}})))
		self.assertIsNone(rewritten_content(""))
		self.assertIsNone(rewritten_content("not json at all"))
