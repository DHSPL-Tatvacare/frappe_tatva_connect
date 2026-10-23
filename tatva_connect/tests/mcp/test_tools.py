# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Each tool's core answer: the handbook, the schema and the settings it reads. Read-only; the handbook tests skip on a
site with no published Wiki space."""
import unittest

import frappe

from tatva_connect.mcp import ToolError, docs, partner_api, schema, settings


class TestHandbookTools(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		if not frappe.db.exists("DocType", "Wiki Space") or not docs._spaces():
			raise unittest.SkipTest("no Wiki space published on this site")
		cls.spaces = docs.list_docs({})["spaces"]
		cls.page = next((entry for space in cls.spaces for entry in docs.list_docs({"space": space["route"]})["contents"]
		                 if not entry["section"]), None)

	def test_the_site_map_lists_spaces_only_and_a_space_lists_its_pages(self):
		self.assertTrue(self.spaces and all("contents" not in space for space in self.spaces))
		with self.assertRaises(ToolError):
			docs.list_docs({"space": "not-a-space"})

	def test_a_page_reads_with_its_url_and_never_carries_an_image_blob(self):
		answer = docs.get_doc({"route": self.page["route"]})
		self.assertEqual((answer["title"], answer["url"]), (self.page["title"], self.page["url"]))
		self.assertNotIn("data:image", answer["text"])

	def test_search_is_wikis_own_index_and_is_bounded(self):
		hits = docs.search_docs({"query": max(self.page["title"].split(), key=len)})["hits"]
		self.assertIn(self.page["route"], [hit["route"] for hit in hits])
		self.assertLessEqual(len(hits), settings.config()["search_max_hits"])
		self.assertTrue(all(hit["score"] is not None for hit in hits))


class TestSchemaTools(unittest.TestCase):
	def test_a_doctype_comes_with_its_fields_relations_rights_and_desk_urls_never_rows(self):
		answer = schema.get_schema({"doctype": "CRM Task Type"})
		self.assertEqual(set(answer), {"doctype", "is_single", "is_child_table", "urls", "you_can", "rows_readable", "fields", "relations"})
		self.assertEqual({t["doctype"] for t in answer["relations"]["child_tables"]},
		                 {f.options for f in frappe.get_meta("CRM Task Type").get_table_fields()})
		self.assertIn("CRM Task", {entry["doctype"] for entry in answer["relations"]["linked_from"]})
		self.assertTrue(answer["urls"]["list"].endswith("/desk/crm-task-type"))

	def test_a_child_table_is_described_through_its_parent(self):
		self.assertTrue(schema.get_schema({"doctype": "CRM Task Type Field"})["is_child_table"])

	def test_a_schema_search_stays_in_scope_and_says_when_it_was_cut(self):
		found, scope = schema.search_schema({"query": "a"}), set(schema._in_scope())
		cap = settings.config()["schema_max_hits"]
		self.assertTrue(found["more"])
		self.assertLessEqual(len(found["doctypes"]), cap)
		self.assertTrue({f["doctype"] for f in found["fields"]} <= scope)


class TestPartnerApiTools(unittest.TestCase):
	def test_an_endpoint_is_found_by_words_and_read_with_its_docs_address(self):
		found = partner_api.search_api({"query": "create lead"})["endpoints"]
		self.assertEqual(found[0]["operation"], "lead_create")
		endpoint = partner_api.get_api({"operation": "lead_create"})
		self.assertTrue(endpoint["url"].endswith("/docs/partner-api/leads#" + partner_api._anchor(endpoint["summary"])))
		self.assertTrue(endpoint["errors"] and endpoint["request_body"])
		with self.assertRaises(ToolError):
			partner_api.get_api({"operation": "no_such_endpoint"})

	def test_the_guides_are_a_docs_space_read_as_plain_markdown(self):
		self.assertIn(partner_api.SPACE, [space["route"] for space in docs.list_docs({})["spaces"]])
		page = docs.get_doc({"route": f"{partner_api.SPACE}/partner-quickstart"})
		self.assertEqual(page["title"], "Quickstart")
		self.assertNotRegex(page["text"], r"<[A-Z][A-Za-z]+")


class TestSettings(unittest.TestCase):
	def test_every_default_in_code_is_the_default_on_the_form(self):
		on_form = {f.fieldname.removeprefix(settings.PREFIX): f.default for f in frappe.get_meta(settings.SETTINGS).fields
		           if f.fieldtype == "Int" and f.fieldname.startswith(settings.PREFIX)}
		self.assertEqual({k: str(v) for k, v in settings.DEFAULTS.items()}, {k: str(v) for k, v in on_form.items()})
