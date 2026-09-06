# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The MCP endpoint — Streamable HTTP, one door, read-only.

ONE URL takes every call: an agent POSTs a single JSON-RPC message and gets one JSON object back.
No SSE stream is offered and no session id is issued, so nothing is held open — the replica sees
short reads and the connection goes straight back to the pool. A GET is answered 405, which the
transport spec allows precisely for a server with no stream to give.

The split here is deliberate. `handle()` is the protocol and knows nothing about Frappe's request
object; `endpoint()` is the thin adapter that reads `frappe.request` and writes the raw response.
That is what lets the whole transport be tested without HTTP.

Authentication is Frappe's own and is never re-implemented: `Authorization: token <key>:<secret>`
is validated before this module runs, and the call executes as that user under that user's
permissions. An unauthenticated caller never reaches here at all — Frappe refuses a non-guest
whitelisted method — so the 401 the protocol requires is stamped by `challenge_unauthenticated`,
an after_request handler, in the same way `normalise_partner_response` stamps the partner contract.

Error shapes: a fault in the ENVELOPE is a JSON-RPC error (parse, malformed, unknown method, bad
arguments). A fault inside a TOOL is a normal result marked `isError` — the protocol is explicit
that a tool which fails is a result, not a transport failure, so the agent can read the reason and
try something else. The messages are machine-facing and deliberately not translated.
"""
import json
from urllib.parse import urlparse

import frappe
from frappe.utils import get_url

from tatva_connect import automation
from tatva_connect.mcp import settings, tools
from tatva_connect.utils import spend_rate_limit

# Derived, never typed: the same shape observability builds for a watched module.
PATH = f"/api/method/{__name__}."
ENABLEMENT_KEY = "MCP::Docs::server"

SERVER_NAME = "tatva-crm-docs"
SERVER_VERSION = "1.0.0"

# Every protocol revision this server can speak. The newest is what an unknown client is offered.
SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
LATEST_VERSION = SUPPORTED_VERSIONS[0]
# The transport spec: a request with no version header is treated as the revision before the header existed.
ASSUMED_VERSION = "2025-03-26"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
NOT_ENABLED = -32001
RATE_LIMITED = -32002
UNAUTHENTICATED = -32003

# THE error vocabulary, declared once as `api/_base.ERROR_CODES` is — JSON-RPC's own four, then ours in the -32000 range the spec reserves.
ERROR_CODES = {
	PARSE_ERROR: "the body was not JSON",
	INVALID_REQUEST: "the message was not a single well-formed JSON-RPC request",
	METHOD_NOT_FOUND: "no such protocol method",
	INVALID_PARAMS: "the params were wrong for that method",
	INTERNAL_ERROR: "a fault on our side",
	NOT_ENABLED: "the operator has not switched this server on",
	RATE_LIMITED: "the caller's own budget is spent",
	UNAUTHENTICATED: "no key, or a key this site does not accept",
}


def _checked_code(code):
	"""THE closed-vocabulary gate. An agent branches on this number, so a code we emit and never
	declare is a lie by omission. Never raises — it runs on the failure path — it degrades and logs."""
	if code in ERROR_CODES:
		return code
	frappe.logger("mcp").error(f"undeclared MCP error code {code!r}")
	return INTERNAL_ERROR


def _error(rid, code, message):
	return {"jsonrpc": "2.0", "id": rid, "error": {"code": _checked_code(code), "message": message}}


def _result(rid, result):
	return {"jsonrpc": "2.0", "id": rid, "result": result}


def _origin_allowed(origin):
	"""DNS-rebinding guard the transport spec requires. No Origin (every non-browser client) passes."""
	if not origin:
		return True
	try:
		return urlparse(origin).netloc.lower() == urlparse(get_url()).netloc.lower()
	except Exception:
		return False


def _charge(cfg):
	"""Two counters, one helper: this caller's own budget, and the ceiling everyone shares.

	The shared one is what stops the site being hammered — a hundred agents each inside their own
	budget are not. `0` on either is the operator saying unlimited, and is honoured as written."""
	for scope, ident, limit in (("mcp-rl:user", frappe.session.user, cfg["per_user_rate"]),
	                            ("mcp-rl:all", "site", cfg["global_rate"])):
		if limit:
			spend_rate_limit(scope, ident, limit, cfg["window_seconds"], "Too many requests.")


def handle(http_method, raw_body, headers):
	"""The whole protocol, as data in and data out: returns (status, headers, payload-or-None)."""
	if (http_method or "").upper() != "POST":
		return 405, {"Allow": "POST"}, None

	if not _origin_allowed(headers.get("Origin")):
		return 403, {}, _error(None, INVALID_REQUEST, "Origin not allowed.")

	version = headers.get("MCP-Protocol-Version") or ASSUMED_VERSION
	if version not in SUPPORTED_VERSIONS:
		return 400, {}, _error(None, INVALID_REQUEST, f"Unsupported MCP-Protocol-Version: {version}")

	if not automation.is_enabled(ENABLEMENT_KEY):
		return 503, {}, _error(None, NOT_ENABLED, "This server is not enabled on this site.")

	cfg = settings.config()
	try:
		_charge(cfg)
	except frappe.RateLimitExceededError:
		return 429, {"Retry-After": str(cfg["window_seconds"])}, _error(
			None, RATE_LIMITED, "Too many requests. Wait for the window to roll over.")

	try:
		message = json.loads(raw_body or b"")
	except Exception:
		return 400, {}, _error(None, PARSE_ERROR, "Body is not valid JSON.")

	# The transport carries ONE message per POST — JSON-RPC batching was removed from the spec.
	if isinstance(message, list):
		return 400, {}, _error(None, INVALID_REQUEST, "Send one JSON-RPC message per request.")
	if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
		return 400, {}, _error(None, INVALID_REQUEST, "Not a JSON-RPC 2.0 message.")

	# A notification (or a response) carries no id and is acknowledged with an empty 202.
	if "id" not in message or not message.get("method"):
		return 202, {}, None

	return 200, {}, _dispatch(message)


def _dispatch(message):
	"""One JSON-RPC request to one answer. Every method this server knows lives in this table."""
	rid, method, params = message.get("id"), message.get("method"), message.get("params") or {}
	if not isinstance(params, dict):
		return _error(rid, INVALID_PARAMS, "params must be an object.")

	if method == "initialize":
		asked = params.get("protocolVersion")
		return _result(rid, {
			"protocolVersion": asked if asked in SUPPORTED_VERSIONS else LATEST_VERSION,
			"capabilities": {"tools": {}},
			"serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
			"instructions": tools.instructions(),
		})

	if method == "ping":
		return _result(rid, {})

	if method == "tools/list":
		return _result(rid, {"tools": tools.listing()})

	if method == "tools/call":
		return _call_tool(rid, params)

	return _error(rid, METHOD_NOT_FOUND, f"Unknown method: {method}")


def _call_tool(rid, params):
	"""Run one tool. Its refusal is a RESULT marked isError, never a transport fault."""
	tool = tools.find(params.get("name"))
	if not tool:
		return _error(rid, INVALID_PARAMS, f"Unknown tool: {params.get('name')}")

	arguments = params.get("arguments") or {}
	if not isinstance(arguments, dict):
		return _error(rid, INVALID_PARAMS, "arguments must be an object.")

	try:
		answer = tool.handler(arguments)
		result = {"content": [{"type": "text", "text": tools.as_text(answer)}], "isError": False}
		# A structured answer goes back in both shapes: the text block every client reads, and structuredContent for one that parses.
		if isinstance(answer, dict):
			result["structuredContent"] = answer
		return _result(rid, result)
	except tools.ToolError as refusal:
		return _result(rid, {"content": [{"type": "text", "text": str(refusal)}], "isError": True})
	except Exception:
		# The traceback goes to a FILE: an Error Log row is a master write inside a replica-switched request.
		frappe.logger("mcp").error(f"tool {tool.name} failed", exc_info=True)
		return _result(rid, {"content": [{"type": "text", "text": "That tool failed. Try another route."}],
		                     "isError": True})


@frappe.whitelist()
@frappe.read_only()
def endpoint():
	"""The single MCP endpoint. Read-only for the whole request — one switch, inherited by every tool.

	The body is written onto `frappe.local.response` and nothing is returned — the app's own response
	contract (`api/_base` §response contract): handler.py adds `message` only to a RETURNED value, so
	what ships is exactly the JSON-RPC envelope. Replacing `frappe.local.response` with a werkzeug
	Response emits the same bytes and breaks the request logger, which reads that dict with `.get` —
	measured, as a 500 on every call once observability watched this path."""
	request = frappe.request
	status, headers, payload = handle(request.method, request.get_data(), request.headers)
	frappe.local.response.update(payload or {})
	frappe.local.response["http_status_code"] = status
	for header, value in (headers or {}).items():
		frappe.local.response_headers[header] = value


def challenge_unauthenticated(response=None, request=None):
	"""after_request: answer an unauthenticated MCP call with 401 and a challenge, not Frappe's 403.

	Frappe refuses a Guest before the endpoint runs, so the protocol's 401 can only be stamped here.
	A 403 earned by a signed-in user is a real permission refusal and is left exactly as it is."""
	try:
		if response is None:
			return
		if request is None:
			request = getattr(frappe, "request", None)
		if request is None:
			return
		if not (getattr(request, "path", "") or "").startswith(PATH):
			return
		if response.status_code not in (401, 403):
			return
		if response.status_code == 403 and getattr(frappe.session, "user", None) not in (None, "Guest"):
			return
		response.status_code = 401
		response.headers["WWW-Authenticate"] = f'Bearer realm="{SERVER_NAME}"'
		response.headers["Content-Type"] = "application/json"
		response.set_data(json.dumps(_error(None, UNAUTHENTICATED, "Authentication required.")))
	except Exception:
		frappe.logger("mcp").error("challenge_unauthenticated failed", exc_info=True)
