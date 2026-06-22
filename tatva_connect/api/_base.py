"""Shared foundation for the gated partner API — entity-agnostic plumbing.

The partner contract is config-driven by ONE `CRM Lead API Mapping` row per partner
(partner_user, enabled, source, vertical, crm_group, program, allowed_programs,
allowed_fields). That SINGLE enabled mapping + its grain governs EVERY entity API —
leads, activities, files, calls — via the SAME `_resolve_caller`. A partner enabled
for a grain can use every entity API for that grain with the SAME API key. There is
NO per-entity enablement; this module is the one brain all entity modules call.

Holds (moved verbatim from partner.py, behaviour-preserving):
  * `_resolve_caller`  — (user, mapping-or-None, is_sysmgr); 403 if neither
  * `_norm_phone`      — phone normaliser
  * `_ok` / `_fail`    — unified success / failure response writers
  * `_classify` + `_ERROR_MAP` — exception -> (code, http, message, fields)
  * `_api`             — endpoint decorator (rate limit + unified-error wrapper)
  * `_rate_limit_retry`— per-key count-based limiter (100 req / 60s)
  * `_run_bulk`        — per-record savepoint -> partial success
  * `_read_list`       — parse a JSON-list request arg
  * `normalise_partner_response` — after_request gateway-error normaliser
  * `resolve_lead`     — the ONE grain-scoped lead resolver every entity API calls
"""
import functools

import frappe
from frappe import _

# Limits ---------------------------------------------------------------------
BULK_MAX = 100          # records per bulk call
RATE_LIMIT = 100        # requests per window, per API key, across all endpoints
RATE_WINDOW = 60        # seconds


# -- caller resolution -------------------------------------------------------

def _norm_phone(raw):
	if not raw:
		return raw
	d = "".join(c for c in str(raw) if c.isdigit())
	return ("+91" + d) if len(d) == 10 else (("+" + d) if d else raw)


def _resolve_caller():
	"""(user, mapping-or-None, is_sysmgr). Raises 403 if neither partner nor sysmgr.
	The mapping row's name == partner_user (autoname field:partner_user).

	This is the SINGLE enablement gate for ALL entity APIs (leads/activities/files/
	calls): one enabled `CRM Lead API Mapping` row + its grain governs every entity."""
	user = frappe.session.user
	mp = frappe.db.get_value(
		"CRM Lead API Mapping", {"partner_user": user, "enabled": 1},
		["source", "vertical", "crm_group", "program"], as_dict=True,
	)
	is_sysmgr = "System Manager" in frappe.get_roles(user)
	if not mp and not is_sysmgr:
		frappe.throw(_("Not authorised: no CRM Lead API Mapping for {0}").format(user), frappe.PermissionError)
	return user, mp, is_sysmgr


def resolve_lead(mp, is_sysmgr, data):
	"""Resolve ONE lead's CRM name from a payload, grain-scoped. The shared lead-
	resolution brain every entity API (activity/file/call) calls so an entity always
	attaches to a lead the caller is actually scoped to.

	Resolves by `data["lead"]` (a CRM Lead name) OR `data["mobile_no"]`:
	  * partner mapping -> FORCE custom_vertical=mp.vertical, custom_group=mp.crm_group
	    so a partner can only reach leads on their own line/group.
	  * trusted sysmgr (no mapping) -> the lead as-is, unscoped.
	Missing AND out-of-scope both raise the SAME generic not-found (no probing)."""
	filters = {}
	if data.get("lead"):
		filters["name"] = data.get("lead")
	elif data.get("mobile_no"):
		filters["mobile_no"] = _norm_phone(data.get("mobile_no"))
	else:
		frappe.throw(_("lead or mobile_no is required"))
	if mp:
		filters["custom_vertical"] = mp.vertical
		filters["custom_group"] = mp.crm_group
	lead_name = frappe.db.get_value("CRM Lead", filters, "name")
	if not lead_name:
		frappe.throw(_("Lead not found"), frappe.DoesNotExistError)
	return lead_name


# -- request-arg helpers -----------------------------------------------------

def _read_list(data, key):
	"""Parse a request arg that should be a JSON list."""
	val = data.get(key)
	if val is None:
		return None
	if isinstance(val, str):
		val = frappe.parse_json(val)
	if not isinstance(val, list):
		val = [val]
	return val


# -- response contract -------------------------------------------------------
# Every endpoint emits a TOP-LEVEL body (no {"message": ...} wrapper): it writes
# frappe.local.response and returns None (handler.py only adds "message" when a
# value is returned; build_response drops the empty "docs" key). Success ->
# {"status":"success",...}; failure -> {"status":"error","error":{code,message}}.

def _ok(action=None, data=None, **extra):
	resp = {"status": "success"}
	if action:
		resp["action"] = action
	if data is not None:
		resp["data"] = data
	resp.update(extra)
	frappe.local.response.update(resp)


def _fail(code, message, http, **extra):
	frappe.clear_messages()
	frappe.local.error_log = []
	err = {"code": code, "message": message}
	err.update(extra)
	frappe.local.response.update({"status": "error", "error": err})
	frappe.local.response["http_status_code"] = http


# -- error mapping + rate limit ----------------------------------------------
# Partners get the unified error contract, never a raw Frappe traceback. The
# exception type maps to a stable code + HTTP status; the message is the throw()'s
# own text (we author those — safe), and anything unexpected is logged server-side
# and returned generically so internals never leak.
_ERROR_MAP = {
	frappe.PermissionError: ("forbidden", 403),
	frappe.DoesNotExistError: ("not_found", 404),
	frappe.ValidationError: ("validation_error", 400),
}
if hasattr(frappe, "DuplicateEntryError"):
	_ERROR_MAP[frappe.DuplicateEntryError] = ("duplicate", 409)
if hasattr(frappe, "RateLimitExceededError"):
	_ERROR_MAP[frappe.RateLimitExceededError] = ("rate_limited", 429)


def _classify(e, fn_name):
	"""(code, http, message, fields) for an exception. Authored throws keep their
	text; a child-write validation error carries the offending `fields` (else None);
	anything unexpected is logged and returned generically as a 500."""
	# A delete blocked by linked activity (LinkExistsError) -> a 409 conflict with a
	# GENERIC message: its native text names the linking doctypes/docs, which would
	# enumerate what exists on the line — so we replace it (never leak the link list).
	if isinstance(e, frappe.LinkExistsError):
		return "cannot_delete", 409, _("This lead cannot be deleted because it has linked records."), None
	for exc_type, (code, http) in _ERROR_MAP.items():
		if isinstance(e, exc_type):
			return code, http, (str(e) or _("Request failed")), getattr(e, "fields", None)
	frappe.log_error(title="Partner API error: {0}".format(fn_name))
	return "server_error", 500, _("Something went wrong. Please try again or contact support."), None


def _rate_limit_retry():
	"""100 requests / 60s PER API KEY, across all endpoints (each partner has its own
	independent budget). Count-based per-key via frappe.cache — Frappe's native limiter
	is site-wide and CPU-time-based, the wrong tool here. Returns the real seconds-to-
	reset if the caller is over the limit, else None."""
	key = frappe.cache.make_key("partner_rl:{0}".format(frappe.session.user))
	count = frappe.cache.incrby(key, 1)
	# Self-heal: incrby+expire is not atomic, so a crash/race between them can leave
	# the counter with NO expire (ttl -1 in redis) -> it never resets -> the key 429s
	# forever. On EVERY call, re-arm the expire whenever the ttl is missing/negative.
	ttl = frappe.cache.ttl(key)
	if ttl is None or ttl < 0:
		frappe.cache.expire(key, RATE_WINDOW)
	if count > RATE_LIMIT:
		ttl = frappe.cache.ttl(key)
		return ttl if (ttl and ttl > 0) else RATE_WINDOW
	return None


def _api(fn):
	"""Wrap an endpoint: enforce the rate limit, run it, and emit the unified contract
	on any failure (never a traceback). The endpoint writes its success body via
	_ok() and returns None — so the response is always top-level."""
	@functools.wraps(fn)
	def wrapper(*args, **kwargs):
		try:
			retry = _rate_limit_retry()
			if retry is not None:
				return _fail(
					"rate_limited",
					_("Rate limit exceeded ({0} requests/min). Retry in {1}s.").format(RATE_LIMIT, retry),
					429, retry_after=retry,
				)
			return fn(*args, **kwargs)
		except Exception as e:
			code, http, message, fields = _classify(e, fn.__name__)
			if fields:
				_fail(code, message, http, fields=fields)
			else:
				_fail(code, message, http)
	return wrapper


def _run_bulk(items, fn):
	"""Run `fn(index, item)` per record in its own savepoint -> partial success.
	A failing record is rolled back and reported; the rest still commit."""
	if not isinstance(items, list):
		frappe.throw(_("Expected a JSON array"))
	if len(items) > BULK_MAX:
		frappe.throw(_("Max {0} records per call; received {1}. Page the rest.").format(BULK_MAX, len(items)))
	results, ok = [], 0
	for i, item in enumerate(items):
		sp = "tc_bulk_{0}".format(i)
		frappe.db.savepoint(sp)
		try:
			results.append(fn(i, item))
			ok += 1
		except Exception as e:
			frappe.db.rollback(save_point=sp)
			code, _http, message, fields = _classify(e, "bulk")
			# the throw populated message_log -> clear it so build_response doesn't
			# leak `_server_messages` into the (otherwise clean) bulk envelope.
			frappe.clear_messages()
			frappe.local.message_log = []
			err = {"code": code, "message": message}
			if fields:
				err["fields"] = fields
			results.append({"index": i, "status": "error", "error": err})
	_ok(summary={"total": len(items), "succeeded": ok, "failed": len(items) - ok}, results=results)


# -- gateway-error normaliser ------------------------------------------------
# Frappe validates the API key and parses the body in its OWN request layer
# (app.py: validate_auth / make_form_dict), BEFORE our endpoint runs — so a bad
# key, a malformed body, or a not-whitelisted call never reaches _api and comes
# back in Frappe's raw {exc_type, exc, _server_messages} shape. This `after_request`
# hook (app.py runs it AFTER handle_exception, with the response object) rewrites
# ANY error on a partner-API path into our unified contract. Success responses,
# our own already-contract errors, and every non-partner path pass through untouched.
# The prefix is GENERALISED (no trailing dot) so it covers every entity module that
# shares it: partner, partner_activity, partner_file, partner_call.
_PARTNER_PATH = "/api/method/tatva_connect.api.partner"

_GATEWAY_ERRORS = {
	"AuthenticationError": ("unauthorized", 401, "Invalid or missing API key."),
	"PermissionError": ("forbidden", 403, "Not permitted."),
}


def _normalise_partner_error(request, status_code, exc_type):
	"""(code, http, message) for a framework-layer error on a partner path."""
	if exc_type in _GATEWAY_ERRORS:
		return _GATEWAY_ERRORS[exc_type]
	if "JSONDecode" in (exc_type or "") or status_code == 400:
		return "bad_request", 400, "Malformed request body."
	if status_code == 401:
		return "unauthorized", 401, "Invalid or missing API key."
	if status_code == 403:
		return "forbidden", 403, "Not permitted."
	if status_code == 404:
		return "not_found", 404, "Not found."
	if status_code == 429:
		return "rate_limited", 429, "Rate limit exceeded. Retry shortly."
	return "server_error", status_code or 500, "Request could not be processed."


def normalise_partner_response(response=None, request=None):
	"""after_request hook (registered in hooks.py). Make EVERY partner-API error —
	including the framework-layer ones (bad key / malformed body / not-whitelisted) —
	speak our contract. Never raises (Frappe logs after_request failures, non-fatal)."""
	try:
		import json
		if response is None:
			return
		# The earliest pre-handler errors (bad key, malformed body) fire before Frappe
		# binds the request kwarg -> fall back to frappe.request so they STILL get the
		# contract instead of leaking Frappe's raw {exc_type, _server_messages} shape.
		if request is None:
			request = getattr(frappe, "request", None)
		if request is None:
			return
		if not (getattr(request, "path", "") or "").startswith(_PARTNER_PATH):
			return
		if response.status_code < 400:
			return
		try:
			body = json.loads(response.get_data(as_text=True) or "{}")
		except Exception:
			body = {}
		if isinstance(body, dict) and body.get("status") == "error":
			return  # already our contract (an _api/_fail response) — leave it
		code, http, message = _normalise_partner_error(
			request, response.status_code, (body or {}).get("exc_type")
		)
		response.status_code = http
		response.set_data(json.dumps({"status": "error", "error": {"code": code, "message": _(message)}}))
		response.headers["Content-Type"] = "application/json"
	except Exception:
		frappe.logger().error("normalise_partner_response failed", exc_info=True)
