# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The two closed vocabularies — the words this server is allowed to use.

The repo's own pattern: a vocabulary is declared once and gated, so a new word cannot be invented
quietly (`api/_base.ERROR_CODES` + `checked_code`, `automation/registry.assert_valid_key`). These
tests are that gate's proof — that the catalog obeys its verbs, that every argument is described in
exactly one place, and that a code we emit is a code we declared.
"""
import unittest

from tatva_connect.mcp import server, tools


class TestToolVocabulary(unittest.TestCase):
	def test_every_tool_is_verb_then_subject(self):
		for tool in tools.REGISTRY:
			verb, _sep, subject = tool.name.partition("_")
			self.assertIn(verb, tools.VERBS, f"{tool.name} does not start with a declared verb")
			self.assertTrue(subject, f"{tool.name} names no subject")

	def test_a_tool_with_an_unnamed_verb_cannot_be_built(self):
		with self.assertRaises(ValueError):
			tools.Tool(name="fetch_docs", description="x", handler=lambda _a: "")

	def test_a_tool_cannot_invent_an_argument(self):
		with self.assertRaises(ValueError):
			tools.Tool(name="get_doc", description="x", handler=lambda _a: "", arguments=("wibble",))

	def test_a_tool_cannot_require_what_it_does_not_accept(self):
		with self.assertRaises(ValueError):
			tools.Tool(name="get_doc", description="x", handler=lambda _a: "", required=("route",))

	def test_an_argument_is_described_in_exactly_one_place(self):
		for tool in tools.REGISTRY:
			for name in tool.arguments:
				self.assertIs(tool.schema()["properties"][name], tools.ARGUMENTS[name])

	def test_every_declared_argument_is_actually_offered_by_some_tool(self):
		offered = {name for tool in tools.REGISTRY for name in tool.arguments}
		self.assertEqual(set(tools.ARGUMENTS) - offered, set(), "an argument is declared but unused")

	def test_every_tool_has_a_description_and_a_handler(self):
		for tool in tools.REGISTRY:
			self.assertTrue(tool.description.strip())
			self.assertTrue(callable(tool.handler))

	def test_tool_names_are_unique(self):
		names = [tool.name for tool in tools.REGISTRY]
		self.assertEqual(len(names), len(set(names)))


class TestErrorVocabulary(unittest.TestCase):
	def test_every_code_the_server_names_is_declared(self):
		named = {value for key, value in vars(server).items()
		         if key.isupper() and isinstance(value, int) and value < 0}
		self.assertEqual(named - set(server.ERROR_CODES), set(), "a code constant is not in ERROR_CODES")

	def test_every_declared_code_carries_its_meaning(self):
		for code, meaning in server.ERROR_CODES.items():
			self.assertTrue(meaning.strip(), f"{code} is declared with no meaning")

	def test_an_undeclared_code_degrades_instead_of_shipping(self):
		payload = server._error(1, -99999, "something")
		self.assertEqual(payload["error"]["code"], server.INTERNAL_ERROR)

	def test_a_declared_code_passes_through_untouched(self):
		payload = server._error(1, server.NOT_ENABLED, "off")
		self.assertEqual(payload["error"]["code"], server.NOT_ENABLED)


class TestSettingsDefaults(unittest.TestCase):
	"""The knobs are declared twice — in `settings.DEFAULTS` and on the form — as the partner API's are.

	Two numbers for one setting can drift, and the drift is invisible: the form would show one value
	while a blank field fell back to another. This holds them equal.
	"""

	def test_every_default_in_code_is_the_default_on_the_form(self):
		import frappe

		from tatva_connect.mcp import settings

		on_form = {field.fieldname: field.default
		           for field in frappe.get_meta(settings.SETTINGS).fields if field.fieldtype == "Int"}
		self.assertEqual(set(on_form), set(settings.DEFAULTS), "a knob exists in one place only")
		for name, default in settings.DEFAULTS.items():
			self.assertEqual(str(on_form[name]), str(default), f"{name} differs between code and form")
