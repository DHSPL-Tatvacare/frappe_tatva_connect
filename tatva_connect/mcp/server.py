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
permissions.
"""
import json
from urllib.parse import urlparse

import frappe
from frappe.utils import get_url

from tatva_connect import automation
from tatva_connect.api._base import ERROR_CODES, error_object, gateway_error
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

# JSON-RPC's own reserved numbers; the server range it leaves us is -32000 to -32099.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Every partner-contract code this server speaks, and the JSON-RPC number it travels under.
RPC_CODES = {
	"bad_request": INVALID_REQUEST,
	"not_found": METHOD_NOT_FOUND,
	"validation_error": INVALID_PARAMS,
	"server_error": INTERNAL_ERROR,
	"unauthorized": -32001,
	"forbidden": -32003,
	"rate_limited": -32029,
	"server_busy": -32005,
}
# Import-time gate, as tools.VERBS is: a code the partner contract does not declare cannot be spoken here.
if undeclared := set(RPC_CODES) - ERROR_CODES:
	raise ValueError(f"RPC_CODES names {sorted(undeclared)}, which api/_base.ERROR_CODES does not declare.")

# The sentence each refusal outside a tool carries on this server, by the code `gateway_error` gives it.
GATEWAY_MESSAGES = {
	"bad_request": "The request was refused as malformed. Send one JSON-RPC message as a JSON body.",
	"unauthorized": "Authentication required. Send `Authorization: token <api_key>:<api_secret>`.",
	"forbidden": "This login may not use the documentation server.",
	"not_found": f"Nothing answers at this address. Call {PATH}endpoint.",
	"rate_limited": "Too many requests. Retry after the number of seconds in the Retry-After header.",
	"server_error": "The request failed before it reached the server. Retry; if it repeats, contact support.",
}


def _error(rid, code, message, rpc=None):
	"""THE error envelope: JSON-RPC's shape, carrying the partner contract's `error_object` as its `data`."""
	data = error_object(code, message)
	error = {"code": rpc or RPC_CODES[code], "message": data["message"], "data": data}
	return {"jsonrpc": "2.0", "id": rid, "error": error}


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
		return 405, {"Allow": "POST"}, _error(None, "bad_request", "Use POST: this server offers no stream.")

	if not _origin_allowed(headers.get("Origin")):
		return 403, {}, _error(None, "forbidden", "Origin not allowed.")

	version = headers.get("MCP-Protocol-Version") or ASSUMED_VERSION
	if version not in SUPPORTED_VERSIONS:
		return 400, {}, _error(None, "bad_request", f"Unsupported MCP-Protocol-Version: {version}")

	if not automation.is_enabled(ENABLEMENT_KEY):
		return 403, {}, _error(None, "forbidden", "This server is not enabled on this site.")

	cfg = settings.config()
	try:
		_charge(cfg)
	except frappe.RateLimitExceededError:
		return 429, {"Retry-After": str(cfg["window_seconds"])}, _error(
			None, "rate_limited", GATEWAY_MESSAGES["rate_limited"])

	try:
		message = json.loads(raw_body or b"")
	except Exception:
		return 400, {}, _error(None, "bad_request", "Body is not valid JSON.", rpc=PARSE_ERROR)

	# The transport carries ONE message per POST — JSON-RPC batching was removed from the spec.
	if isinstance(message, list):
		return 400, {}, _error(None, "bad_request", "Send one JSON-RPC message per request.")
	if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
		return 400, {}, _error(None, "bad_request", "Not a JSON-RPC 2.0 message.")

	# A notification (or a response) carries no id and is acknowledged with an empty 202.
	if "id" not in message or not message.get("method"):
		return 202, {}, None

	return 200, {}, _dispatch(message)


def _dispatch(message):
	"""One JSON-RPC request to one answer. Every method this server knows lives in this table."""
	rid, method, params = message.get("id"), message.get("method"), message.get("params") or {}
	if not isinstance(params, dict):
		return _error(rid, "validation_error", "params must be an object.")

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

	return _error(rid, "not_found", f"Unknown method: {method}")


def _call_tool(rid, params):
	"""Run one tool. Its refusal is a RESULT marked isError, never a transport fault."""
	tool = tools.find(params.get("name"))
	if not tool:
		return _error(rid, "validation_error", f"Unknown tool: {params.get('name')}")

	arguments = params.get("arguments") or {}
	if not isinstance(arguments, dict):
		return _error(rid, "validation_error", "arguments must be an object.")

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
	# The envelope is the whole body: a throw's queued message must not ride beside it, as `_fail` clears it too.
	frappe.clear_messages()
	frappe.local.response.update(payload or {})
	frappe.local.response["http_status_code"] = status
	for header, value in (headers or {}).items():
		frappe.local.response_headers[header] = value


def normalise_mcp_response(response=None, request=None):
	"""after_request twin of `normalise_partner_response`: a framework refusal answers in the envelope."""
	try:
		if response is None:
			return
		if request is None:
			request = getattr(frappe, "request", None)
		if request is None:
			return
		if not (getattr(request, "path", "") or "").startswith(PATH):
			return
		if response.status_code < 400:
			return
		body = frappe.parse_json(response.get_data(as_text=True) or "{}")
		if not isinstance(body, dict):
			body = {}
		if body.get("jsonrpc"):
			return  # already our envelope — `endpoint` answered it
		code, http = gateway_error(response.status_code, body.get("exc_type"))
		# No login at all is a missing credential, not a refused one: the transport spec answers it 401.
		if code == "forbidden" and getattr(frappe.session, "user", None) in (None, "Guest"):
			code, http = "unauthorized", 401
		payload = _error(None, code, GATEWAY_MESSAGES[code])
		response.status_code = http
		response.set_data(frappe.as_json(payload))
		response.headers["Content-Type"] = "application/json"
		if code == "unauthorized":
			response.headers["WWW-Authenticate"] = f'token realm="{SERVER_NAME}"'
		# Same reason as the partner twin: observability.log_request, the next hook, reads the verdict from here.
		frappe.local.response["error"] = payload["error"]
	except Exception:
		frappe.logger("mcp").error("normalise_mcp_response failed", exc_info=True)
