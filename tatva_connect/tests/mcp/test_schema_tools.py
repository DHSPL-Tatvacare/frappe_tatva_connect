# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The schema and Desk tools — structure, never a row, and a URL that is built in one place.

Nothing is written. The assertions are about the CONTRACT rather than about any one doctype: that
scope is derived from the allowlisted apps, that a framework doctype is refused however it is asked
for, that what comes back is field structure and not data, and that a route from one tool is an
address the next tool accepts.
"""
import unittest

import frappe

from tatva_connect.mcp import ToolError, desk, schema, settings


class TestSchemaTools(unittest.TestCase):
	def test_scope_is_derived_from_the_allowlisted_apps(self):
		scope = schema._in_scope()
		self.assertIn("CRM Lead", scope)
		self.assertNotIn("User", scope)
		self.assertNotIn("Role", scope)

	def test_a_framework_doctype_is_refused_by_name(self):
		for outside in ("User", "Role", "System Settings"):
			with self.assertRaises(ToolError, msg=outside):
				schema.get_schema({"doctype": outside})

	def test_an_empty_doctype_is_refused_and_points_at_search(self):
		with self.assertRaises(ToolError) as refusal:
			schema.get_schema({"doctype": "  "})
		self.assertIn("search_schema", str(refusal.exception))

	def test_a_doctype_in_scope_describes_its_fields_in_the_words_on_screen(self):
		answer = schema.get_schema({"doctype": "CRM Lead"})
		self.assertEqual(answer["doctype"], "CRM Lead")
		self.assertTrue(answer["fields"])
		for field in answer["fields"]:
			self.assertTrue(field["field"] and field["label"] and field["type"])

	def test_layout_only_fields_are_not_offered_as_things_to_fill_in(self):
		types = {field["type"] for field in schema.get_schema({"doctype": "CRM Lead"})["fields"]}
		self.assertFalse(types & {"Section Break", "Column Break", "Tab Break", "HTML"})

	def test_the_description_carries_the_desk_pages(self):
		urls = schema.get_schema({"doctype": "CRM Lead"})["urls"]
		self.assertTrue(urls["list"].endswith("/app/crm-lead"))
		self.assertTrue(urls["new"].endswith("/app/crm-lead/new"))

	def test_no_row_of_data_can_come_back(self):
		answer = schema.get_schema({"doctype": "CRM Lead"})
		self.assertEqual(set(answer) - {"doctype", "is_single", "urls", "fields"}, set())

	def test_an_empty_query_is_refused(self):
		with self.assertRaises(ToolError):
			schema.search_schema({"query": ""})

	def test_a_word_finds_doctypes_and_fields_within_scope_only(self):
		found = schema.search_schema({"query": "lead"})
		scope = set(schema._in_scope())
		self.assertTrue(found["doctypes"] or found["fields"])
		for name in found["doctypes"]:
			self.assertIn(name, scope)
		for field in found["fields"]:
			self.assertIn(field["doctype"], scope)

	def test_a_search_is_bounded(self):
		found = schema.search_schema({"query": "a"})
		cap = settings.config()["schema_max_hits"]
		self.assertLessEqual(len(found["doctypes"]), cap)
		self.assertLessEqual(len(found["fields"]), cap)

	def test_a_name_from_search_is_readable_by_get_schema(self):
		found = schema.search_schema({"query": "lead"})
		if not found["doctypes"]:
			self.skipTest("no doctype matched")
		self.assertEqual(schema.get_schema({"doctype": found["doctypes"][0]})["doctype"],
		                 found["doctypes"][0])


class TestDeskTools(unittest.TestCase):
	def test_every_url_is_built_from_this_site(self):
		site = frappe.utils.get_url()
		self.assertTrue(desk.list_url("CRM Lead").startswith(site))
		self.assertTrue(desk.workspace_url("Communications").startswith(site))

	def test_a_doctype_slug_matches_the_desk_route(self):
		self.assertTrue(desk.list_url("CRM Telephony Account").endswith("/app/crm-telephony-account"))

	def test_the_map_lists_workspaces_with_an_address(self):
		listed = desk.list_workspaces({})["workspaces"]
		self.assertTrue(listed)
		for entry in listed:
			self.assertTrue(entry["workspace"] and entry["title"] and entry["url"].startswith("http"))

	def test_an_unknown_workspace_is_refused_and_says_what_exists(self):
		with self.assertRaises(ToolError) as refusal:
			desk.get_workspace({"workspace": "Nowhere"})
		self.assertIn("Nowhere", str(refusal.exception))

	def test_an_empty_workspace_is_refused(self):
		with self.assertRaises(ToolError):
			desk.get_workspace({"workspace": ""})

	def test_a_workspace_from_the_map_opens_and_every_entry_has_a_url(self):
		first = desk.list_workspaces({})["workspaces"][0]["workspace"]
		opened = desk.get_workspace({"workspace": first})
		self.assertEqual(opened["workspace"], first)
		for entry in opened["shortcuts"] + opened["links"]:
			self.assertTrue(entry["url"].startswith("http"))
			self.assertTrue(entry["doctype"])
