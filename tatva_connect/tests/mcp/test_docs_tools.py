# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The handbook tools, against the Wiki this site actually holds.

No fixture is created and nothing is written — the tools are read-only and so are their tests. The
assertions are about SHAPE and CONTRACT rather than about any particular page, so they hold on a
site with the handbook published and are skipped on one without it: what must never break is that a
route from one tool is an address the next tool accepts.
"""
import unittest

import frappe

from tatva_connect.mcp import ToolError, docs, settings


class TestDocsTools(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		if not frappe.db.exists("DocType", "Wiki Space") or not docs._spaces():
			raise unittest.SkipTest("no Wiki space published on this site")
		cls.spaces = docs.list_docs({})["spaces"]

	def first_page(self):
		for space in self.spaces:
			for entry in space["contents"]:
				if not entry["section"] and entry["route"]:
					return entry
		self.skipTest("no published page in any space")

	# -- list_docs ------------------------------------------------------------
	def test_the_map_names_every_space_and_addresses_every_entry(self):
		self.assertTrue(self.spaces)
		for space in self.spaces:
			self.assertTrue(space["space"] and space["route"])
			for entry in space["contents"]:
				self.assertTrue(entry["title"])
				self.assertIn("section", entry)

	def test_the_map_can_be_narrowed_to_one_space(self):
		route = self.spaces[0]["route"]
		narrowed = docs.list_docs({"space": route})["spaces"]
		self.assertEqual([s["route"] for s in narrowed], [route])

	def test_an_unknown_space_is_refused_by_name_and_says_what_exists(self):
		with self.assertRaises(ToolError) as refusal:
			docs.list_docs({"space": "not-a-space"})
		self.assertIn("not-a-space", str(refusal.exception))
		self.assertIn(self.spaces[0]["route"], str(refusal.exception))

	# -- search_docs ----------------------------------------------------------
	def test_an_empty_query_is_refused_rather_than_returning_everything(self):
		with self.assertRaises(ToolError):
			docs.search_docs({"query": "   "})

	def test_a_word_from_a_real_title_finds_that_page(self):
		page = self.first_page()
		word = max(page["title"].split(), key=len)
		hits = docs.search_docs({"query": word})["hits"]
		self.assertTrue(hits, f"the indexed search found nothing for {word!r}")
		self.assertIn(page["route"], [hit["route"] for hit in hits])

	def test_the_search_is_wikis_own_indexed_one(self):
		"""Proof of no reinvention: the hits carry the index's score, which only wiki's search returns."""
		hits = docs.search_docs({"query": "lead"})["hits"]
		self.assertTrue(hits)
		self.assertTrue(all(hit["score"] is not None for hit in hits))

	def test_a_search_is_bounded_and_every_hit_is_addressable(self):
		hits = docs.search_docs({"query": "lead"})["hits"]
		self.assertLessEqual(len(hits), settings.config()["search_max_hits"])
		for hit in hits:
			self.assertTrue(hit["route"] and hit["title"])
			self.assertNotIn("data:image", hit["snippet"])

	def test_a_search_can_be_scoped_to_one_space(self):
		route = self.spaces[0]["route"]
		inside = {entry["route"] for entry in self.spaces[0]["contents"]}
		for hit in docs.search_docs({"query": "lead", "space": route})["hits"]:
			self.assertIn(hit["route"], inside)

	# -- get_doc --------------------------------------------------------------
	def test_a_route_from_the_map_is_readable(self):
		page = self.first_page()
		answer = docs.get_doc({"route": page["route"]})
		self.assertEqual(answer["title"], page["title"])
		self.assertGreaterEqual(answer["pages"], 1)
		self.assertEqual(answer["page"], 1)

	def test_a_leading_slash_addresses_the_same_page(self):
		page = self.first_page()
		bare = docs.get_doc({"route": page["route"].strip("/")})
		slashed = docs.get_doc({"route": "/" + page["route"].strip("/")})
		self.assertEqual(bare["route"], slashed["route"])

	def test_images_never_reach_the_agent_as_base64(self):
		for space in self.spaces:
			for entry in space["contents"]:
				if entry["section"] or not entry["route"]:
					continue
				self.assertNotIn("data:image", docs.get_doc({"route": entry["route"]})["text"])

	def test_a_page_number_past_the_end_lands_on_the_last_page(self):
		page = self.first_page()
		answer = docs.get_doc({"route": page["route"], "page": 9999})
		self.assertEqual(answer["page"], answer["pages"])

	def test_a_page_number_that_is_not_a_number_is_refused_in_words(self):
		page = self.first_page()
		with self.assertRaises(ToolError):
			docs.get_doc({"route": page["route"], "page": "last"})

	def test_an_unknown_route_is_refused_and_points_at_search(self):
		with self.assertRaises(ToolError) as refusal:
			docs.get_doc({"route": "no/such/page"})
		self.assertIn("search_docs", str(refusal.exception))

	def test_an_empty_route_is_refused(self):
		with self.assertRaises(ToolError):
			docs.get_doc({"route": ""})
