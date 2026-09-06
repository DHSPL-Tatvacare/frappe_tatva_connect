# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The transport — every rule the MCP Streamable HTTP spec puts on a server with no stream.

These drive `server.handle()` directly rather than over HTTP, because `handle` IS the protocol: it
takes a verb, a body and headers and returns a status, headers and a payload. `endpoint()` is four
lines of adapter around it, so testing the seam tests the contract and not werkzeug.

The enablement flag is mocked ON for every test but the one that proves the dormant state, and the
per-user Redis counter is flushed in setUp so a re-run cannot inherit the previous run's budget.
"""
import json
import unittest
from unittest.mock import patch

import frappe

from tatva_connect.mcp import server, settings, tools

_ENABLED = "tatva_connect.mcp.server.automation.is_enabled"


def _body(**message):
	return json.dumps({"jsonrpc": "2.0", **message}).encode()


class TestMCPTransport(unittest.TestCase):
	def setUp(self):
		frappe.cache.delete_value(f"mcp-rl:user:{frappe.session.user}")
		frappe.cache.delete_value("mcp-rl:all:site")
		self.enabled = patch(_ENABLED, return_value=True)
		self.enabled.start()
		self.addCleanup(self.enabled.stop)

	def call(self, verb="POST", body=b"", headers=None):
		return server.handle(verb, body, headers or {})

	# -- transport rules ------------------------------------------------------
	def test_get_is_405_because_no_stream_is_offered(self):
		status, headers, payload = self.call(verb="GET")
		self.assertEqual(status, 405)
		self.assertEqual(headers.get("Allow"), "POST")
		self.assertIsNone(payload)

	def test_foreign_origin_is_refused(self):
		status, _h, payload = self.call(body=_body(id=1, method="ping"),
		                                headers={"Origin": "https://evil.example.com"})
		self.assertEqual(status, 403)
		self.assertEqual(payload["error"]["code"], server.INVALID_REQUEST)

	def test_absent_origin_is_allowed(self):
		status, _h, _p = self.call(body=_body(id=1, method="ping"))
		self.assertEqual(status, 200)

	def test_unsupported_protocol_version_is_400(self):
		status, _h, payload = self.call(body=_body(id=1, method="ping"),
		                                headers={"MCP-Protocol-Version": "1999-01-01"})
		self.assertEqual(status, 400)
		self.assertIn("Unsupported", payload["error"]["message"])

	def test_missing_version_header_is_accepted_as_the_pre_header_revision(self):
		self.assertIn(server.ASSUMED_VERSION, server.SUPPORTED_VERSIONS)
		status, _h, _p = self.call(body=_body(id=1, method="ping"))
		self.assertEqual(status, 200)

	def test_malformed_body_is_a_parse_error(self):
		status, _h, payload = self.call(body=b"{not json")
		self.assertEqual(status, 400)
		self.assertEqual(payload["error"]["code"], server.PARSE_ERROR)

	def test_a_batch_is_refused_because_the_spec_removed_batching(self):
		status, _h, payload = self.call(body=json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "ping"}]).encode())
		self.assertEqual(status, 400)
		self.assertEqual(payload["error"]["code"], server.INVALID_REQUEST)

	def test_a_notification_is_acknowledged_with_an_empty_202(self):
		status, _h, payload = self.call(body=_body(method="notifications/initialized"))
		self.assertEqual(status, 202)
		self.assertIsNone(payload)

	# -- the dormant state ----------------------------------------------------
	def test_flag_off_answers_not_enabled_and_runs_nothing(self):
		with patch(_ENABLED, return_value=False):
			status, _h, payload = self.call(body=_body(id=1, method="tools/list"))
		self.assertEqual(status, 503)
		self.assertEqual(payload["error"]["code"], server.NOT_ENABLED)
		self.assertNotIn("result", payload)

	# -- the lifecycle --------------------------------------------------------
	def test_initialize_echoes_a_supported_version_and_carries_the_manual(self):
		_s, _h, payload = self.call(body=_body(id=1, method="initialize",
		                                       params={"protocolVersion": server.LATEST_VERSION}))
		result = payload["result"]
		self.assertEqual(result["protocolVersion"], server.LATEST_VERSION)
		self.assertIn("tools", result["capabilities"])
		self.assertEqual(result["serverInfo"]["name"], server.SERVER_NAME)
		self.assertTrue(result["instructions"].strip())

	def test_initialize_offers_the_latest_when_the_client_asks_for_one_we_do_not_speak(self):
		_s, _h, payload = self.call(body=_body(id=1, method="initialize",
		                                       params={"protocolVersion": "1999-01-01"}))
		self.assertEqual(payload["result"]["protocolVersion"], server.LATEST_VERSION)

	def test_tools_list_publishes_every_registered_tool_with_a_schema(self):
		_s, _h, payload = self.call(body=_body(id=1, method="tools/list"))
		published = payload["result"]["tools"]
		self.assertEqual({t["name"] for t in published}, {t.name for t in tools.REGISTRY})
		for entry in published:
			self.assertEqual(entry["inputSchema"]["type"], "object")
			self.assertFalse(entry["inputSchema"]["additionalProperties"])

	def test_unknown_method_is_method_not_found(self):
		_s, _h, payload = self.call(body=_body(id=1, method="resources/list"))
		self.assertEqual(payload["error"]["code"], server.METHOD_NOT_FOUND)

	def test_params_must_be_an_object(self):
		_s, _h, payload = self.call(body=_body(id=1, method="tools/call", params=["guide"]))
		self.assertEqual(payload["error"]["code"], server.INVALID_PARAMS)

	# -- tools ----------------------------------------------------------------
	def test_the_guide_tool_answers_with_a_text_block(self):
		_s, _h, payload = self.call(body=_body(id=1, method="tools/call",
		                                       params={"name": "get_guide", "arguments": {}}))
		result = payload["result"]
		self.assertFalse(result["isError"])
		self.assertEqual(result["content"][0]["type"], "text")
		self.assertIn("READ-ONLY", result["content"][0]["text"])

	def test_the_guide_names_every_registered_tool(self):
		text = tools.instructions()
		for tool in tools.REGISTRY:
			self.assertIn(tool.name, text)

	def test_an_unknown_tool_is_invalid_params(self):
		_s, _h, payload = self.call(body=_body(id=1, method="tools/call",
		                                       params={"name": "drop_database", "arguments": {}}))
		self.assertEqual(payload["error"]["code"], server.INVALID_PARAMS)

	def test_a_tool_refusal_is_a_result_not_a_transport_fault(self):
		refusing = tools.Tool(name="get_guide", description="x",
		                      handler=lambda _a: (_ for _ in ()).throw(tools.ToolError("no such route")))
		with patch("tatva_connect.mcp.server.tools.find", return_value=refusing):
			_s, _h, payload = self.call(body=_body(id=1, method="tools/call",
			                                       params={"name": "get_guide", "arguments": {}}))
		self.assertNotIn("error", payload)
		self.assertTrue(payload["result"]["isError"])
		self.assertIn("no such route", payload["result"]["content"][0]["text"])

	def test_an_unexpected_tool_failure_never_leaks_a_traceback(self):
		exploding = tools.Tool(name="get_guide", description="x",
		                       handler=lambda _a: (_ for _ in ()).throw(RuntimeError("secret internals")))
		with patch("tatva_connect.mcp.server.tools.find", return_value=exploding):
			_s, _h, payload = self.call(body=_body(id=1, method="tools/call",
			                                       params={"name": "get_guide", "arguments": {}}))
		self.assertTrue(payload["result"]["isError"])
		self.assertNotIn("secret internals", payload["result"]["content"][0]["text"])

	# -- rate limiting --------------------------------------------------------
	def _limits(self, **overrides):
		return patch("tatva_connect.mcp.server.settings.config",
		             return_value={**settings.DEFAULTS, **overrides})

	def test_the_caller_is_throttled_on_their_own_budget(self):
		with self._limits(per_user_rate=2):
			for _ in range(2):
				self.assertEqual(self.call(body=_body(id=1, method="ping"))[0], 200)
			status, headers, payload = self.call(body=_body(id=1, method="ping"))
		self.assertEqual(status, 429)
		self.assertTrue(headers.get("Retry-After"))
		self.assertEqual(payload["error"]["code"], server.RATE_LIMITED)

	def test_the_shared_ceiling_throttles_even_a_caller_inside_their_own_budget(self):
		with self._limits(per_user_rate=0, global_rate=1):
			self.assertEqual(self.call(body=_body(id=1, method="ping"))[0], 200)
			status, _h, _p = self.call(body=_body(id=1, method="ping"))
		self.assertEqual(status, 429)

	def test_zero_means_unlimited_on_a_rate(self):
		with self._limits(per_user_rate=0, global_rate=0):
			for _ in range(5):
				self.assertEqual(self.call(body=_body(id=1, method="ping"))[0], 200)

	def test_a_structured_answer_comes_back_structured_as_well_as_text(self):
		with patch("tatva_connect.mcp.server.tools.find",
		           return_value=tools.Tool(name="get_guide", description="x",
		                                   handler=lambda _a: {"spaces": []})):
			_s, _h, payload = self.call(body=_body(id=1, method="tools/call",
			                                       params={"name": "get_guide", "arguments": {}}))
		self.assertEqual(payload["result"]["structuredContent"], {"spaces": []})
		self.assertEqual(payload["result"]["content"][0]["type"], "text")
