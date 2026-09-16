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
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from werkzeug.wrappers import Response

from tatva_connect.api import _base
from tatva_connect.mcp import server, settings, tools

_ENABLED = "tatva_connect.mcp.server.automation.is_enabled"


def _body(**message):
	return json.dumps({"jsonrpc": "2.0", **message}).encode()


def _code(payload):
	"""The partner-contract code an error envelope carries in its `data`."""
	return payload["error"]["data"]["code"]


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
		self.assertEqual(_code(payload), "bad_request")

	def test_foreign_origin_is_refused(self):
		status, _h, payload = self.call(body=_body(id=1, method="ping"),
		                                headers={"Origin": "https://evil.example.com"})
		self.assertEqual(status, 403)
		self.assertEqual(_code(payload), "forbidden")

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
		self.assertEqual(_code(payload), "bad_request")

	def test_a_batch_is_refused_because_the_spec_removed_batching(self):
		status, _h, payload = self.call(body=json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "ping"}]).encode())
		self.assertEqual(status, 400)
		self.assertEqual(_code(payload), "bad_request")

	def test_a_notification_is_acknowledged_with_an_empty_202(self):
		status, _h, payload = self.call(body=_body(method="notifications/initialized"))
		self.assertEqual(status, 202)
		self.assertIsNone(payload)

	# -- the dormant state ----------------------------------------------------
	def test_flag_off_answers_not_enabled_and_runs_nothing(self):
		with patch(_ENABLED, return_value=False):
			status, _h, payload = self.call(body=_body(id=1, method="tools/list"))
		self.assertEqual(status, 403)
		self.assertEqual(_code(payload), "forbidden")
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
		self.assertEqual(_code(payload), "not_found")

	def test_params_must_be_an_object(self):
		_s, _h, payload = self.call(body=_body(id=1, method="tools/call", params=["guide"]))
		self.assertEqual(_code(payload), "validation_error")

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
		self.assertEqual(_code(payload), "validation_error")

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
		self.assertEqual(_code(payload), "rate_limited")

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


class TestErrorEnvelope(unittest.TestCase):
	"""Every error is JSON-RPC's shape carrying the partner contract's `error_object` — one vocabulary, two transports."""

	def test_every_code_is_a_partner_contract_code(self):
		self.assertEqual(set(server.RPC_CODES) - _base.ERROR_CODES, set())

	def test_the_data_is_the_partner_error_object_and_the_number_follows_the_code(self):
		for code, number in server.RPC_CODES.items():
			error = server._error(1, code, "  a   refusal ")["error"]
			self.assertEqual(error["data"], _base.error_object(code, "a refusal"))
			self.assertEqual((error["code"], error["message"]), (number, "a refusal"))

	def test_the_request_log_reads_the_contract_code_not_the_transport_number(self):
		payload = server._error(1, "rate_limited", "slow down")
		with patch.object(frappe.local, "response", frappe._dict(payload)):
			self.assertEqual(_base.request_error()["code"], "rate_limited")

	def test_the_endpoint_body_carries_nothing_beside_the_envelope(self):
		frappe.local.message_log = [{"message": "a queued message a throw left behind"}]
		request = SimpleNamespace(method="GET", get_data=lambda: b"", headers={})
		with patch.object(frappe, "request", request, create=True), \
				patch.object(frappe.local, "response", frappe._dict()), \
				patch.object(frappe.local, "response_headers", {}, create=True):
			server.endpoint()
			self.assertEqual(frappe.local.message_log, [])


class TestGatewayRefusals(unittest.TestCase):
	"""A call the framework refuses before `endpoint` runs still answers in the envelope."""

	path = f"{server.PATH}endpoint"

	def refuse(self, status, exc_type=None, user="Guest", path=None, body=None):
		response = Response(frappe.as_json(body or {"exc_type": exc_type}), status=status)
		request = SimpleNamespace(path=path or self.path)
		with patch.object(frappe.local, "session", frappe._dict(user=user)), \
				patch.object(frappe.local, "response", frappe._dict()):
			server.normalise_mcp_response(response=response, request=request)
			logged = frappe.local.response.get("error")
		return response, json.loads(response.get_data(as_text=True)), logged

	def test_no_login_is_401_with_a_token_challenge_not_bearer(self):
		response, payload, _l = self.refuse(403, "PermissionError")
		self.assertEqual(response.status_code, 401)
		self.assertEqual(_code(payload), "unauthorized")
		self.assertTrue(response.headers["WWW-Authenticate"].startswith("token "))

	def test_a_bad_key_is_401(self):
		response, payload, _l = self.refuse(401, "AuthenticationError")
		self.assertEqual((response.status_code, _code(payload)), (401, "unauthorized"))

	def test_a_signed_in_refusal_stays_403_in_the_envelope(self):
		response, payload, _l = self.refuse(403, "PermissionError", user="someone@example.com")
		self.assertEqual((response.status_code, _code(payload)), (403, "forbidden"))
		self.assertNotIn("WWW-Authenticate", response.headers)

	def test_a_framework_500_is_a_server_error_envelope_and_is_logged_as_one(self):
		response, payload, logged = self.refuse(500, "RuntimeError", user="someone@example.com")
		self.assertEqual((response.status_code, payload["jsonrpc"], _code(payload)), (500, "2.0", "server_error"))
		self.assertEqual(logged["data"]["code"], "server_error")

	def test_our_own_envelope_is_left_untouched(self):
		own = server._error(1, "forbidden", "This server is not enabled on this site.")
		_r, payload, _l = self.refuse(403, body=own, user="someone@example.com")
		self.assertEqual(payload, own)

	def test_another_path_is_left_untouched(self):
		response, payload, _l = self.refuse(403, "PermissionError", path="/api/method/frappe.client.get")
		self.assertEqual((response.status_code, payload), (403, {"exc_type": "PermissionError"}))

	def test_every_code_the_gateway_can_give_has_a_sentence(self):
		given = set(_base._GATEWAY_CODES.values()) | {"server_error"}
		self.assertEqual(given - set(server.GATEWAY_MESSAGES), set())
