# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The transport contract — what any MCP client relies on, driven through `server.handle()` (the protocol, without HTTP).

The flag is mocked ON for every test but the dormant one; the Redis counters are flushed so a re-run starts clean.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from werkzeug.wrappers import Response

from tatva_connect.api import _base
from tatva_connect.mcp import server, settings, tools

_ENABLED = "tatva_connect.mcp.server.automation.is_enabled"


def _body(**message):
	return frappe.as_json({"jsonrpc": "2.0", **message}).encode()


def _code(payload):
	return payload["error"]["data"]["code"]


class TestMCPProtocol(unittest.TestCase):
	def setUp(self):
		frappe.cache.delete_value(f"mcp-rl:user:{frappe.session.user}")
		frappe.cache.delete_value("mcp-rl:all:site")
		self.enabled = patch(_ENABLED, return_value=True)
		self.enabled.start()
		self.addCleanup(self.enabled.stop)

	def call(self, verb="POST", body=b"", headers=None):
		return server.handle(verb, body, headers or {})

	def call_tool(self, name, arguments):
		return self.call(body=_body(id=1, method="tools/call", params={"name": name, "arguments": arguments}))[2]

	def test_the_transport_refuses_what_the_spec_refuses(self):
		ping = _body(id=1, method="ping")
		for case, (status, kwargs) in {
			"no stream": (405, {"verb": "GET"}),
			"foreign origin": (403, {"body": ping, "headers": {"Origin": "https://evil.example.com"}}),
			"unknown version": (400, {"body": ping, "headers": {"MCP-Protocol-Version": "1999-01-01"}}),
			"bad json": (400, {"body": b"{not json"}),
			"batch": (400, {"body": frappe.as_json([{"jsonrpc": "2.0", "id": 1, "method": "ping"}]).encode()}),
		}.items():
			with self.subTest(case):
				self.assertEqual(self.call(**kwargs)[0], status)
		self.assertEqual(self.call(body=_body(method="notifications/initialized"))[:3:2], (202, None))

	def test_flag_off_refuses_every_call(self):
		with patch(_ENABLED, return_value=False):
			status, _h, payload = self.call(body=_body(id=1, method="tools/list"))
		self.assertEqual((status, _code(payload)), (403, "forbidden"))

	def test_initialize_negotiates_the_version_and_carries_the_manual(self):
		result = self.call(body=_body(id=1, method="initialize", params={"protocolVersion": "1999-01-01"}))[2]["result"]
		self.assertEqual(result["protocolVersion"], server.LATEST_VERSION)
		self.assertIn(frappe.utils.get_url(), result["instructions"])
		for tool in tools.REGISTRY:
			self.assertIn(tool.name, result["instructions"], "the manual must name every tool")

	def test_every_tool_is_published_read_only_with_a_closed_schema(self):
		published = self.call(body=_body(id=1, method="tools/list"))[2]["result"]["tools"]
		self.assertEqual({t["name"] for t in published}, {t.name for t in tools.REGISTRY})
		for entry in published:
			self.assertFalse(entry["inputSchema"]["additionalProperties"])
			self.assertIs(entry["annotations"]["readOnlyHint"], True)

	def test_a_wrong_argument_is_named_in_a_result_the_agent_can_correct(self):
		for arguments, said in (({"query": 123}, "query must be a string"), ({"query": "x", "wibble": 1}, "wibble")):
			result = self.call_tool("search_schema", arguments)["result"]
			self.assertTrue(result["isError"])
			self.assertIn(said, result["content"][0]["text"])

	def test_a_tool_failure_is_an_error_result_that_never_leaks_internals(self):
		exploding = tools.Tool(name="get_guide", description="x",
		                       handler=lambda _a: (_ for _ in ()).throw(RuntimeError("secret internals")))
		with patch("tatva_connect.mcp.server.tools.find", return_value=exploding):
			payload = self.call_tool("get_guide", {})
		self.assertNotIn("error", payload)
		self.assertTrue(payload["result"]["isError"])
		self.assertNotIn("secret internals", payload["result"]["content"][0]["text"])

	def test_a_caller_over_their_budget_is_told_when_to_retry(self):
		with patch("tatva_connect.mcp.server.settings.config", return_value={**settings.DEFAULTS, "per_user_rate": 1}):
			self.call(body=_body(id=1, method="ping"))
			status, headers, payload = self.call(body=_body(id=1, method="ping"))
		self.assertEqual((status, _code(payload)), (429, "rate_limited"))
		self.assertTrue(headers.get("Retry-After"))


class TestMCPEnvelope(unittest.TestCase):
	def test_every_error_carries_the_partner_contract_error(self):
		self.assertEqual(set(server.RPC_CODES) - _base.ERROR_CODES, set())
		for code, number in server.RPC_CODES.items():
			error = server._error(1, code, "a refusal")["error"]
			self.assertEqual((error["code"], error["data"]), (number, _base.error_object(code, "a refusal")))

	def test_no_login_is_a_401_in_the_envelope(self):
		response = Response(frappe.as_json({"exc_type": "PermissionError"}), status=403)
		with patch.object(frappe.local, "session", frappe._dict(user="Guest")), \
				patch.object(frappe.local, "response", frappe._dict()):
			server.normalise_mcp_response(response=response, request=SimpleNamespace(path=f"{server.PATH}endpoint"))
		self.assertEqual((response.status_code, _code(frappe.parse_json(response.get_data(as_text=True)))), (401, "unauthorized"))
