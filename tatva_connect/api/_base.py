"""Shared foundation for the gated partner API — entity-agnostic plumbing.

The partner contract is config-driven by ONE `CRM Lead API Mapping` row per partner
(partner_user, enabled, source, vertical, crm_group, program, allowed_programs,
allowed_fields). That SINGLE enabled mapping + its grain governs EVERY entity API —
leads, activities, files, calls — via the SAME `_resolve_caller`. A partner enabled
for a grain can use every entity API for that grain with the SAME API key. There is
NO per-entity enablement; this module is the one brain all entity modules call.

Holds:
  * `_resolve_caller`  — (user, mapping-or-None, is_sysmgr); 403 if neither
  * `_norm_phone`      — phone normaliser
  * `_ok` / `_fail`    — unified success / failure response writers
  * `_classify` + `_ERROR_MAP` — exception -> (code, http, message, fields)
  * `_api`             — endpoint decorator (rate limit + unified-error wrapper)
  * `_cfg`             — fresh read of the CRM Partner API Settings Single (DEFAULTS + 0-rules)
  * `_rate_check`      — per-token + global token-bucket limiter (cost = records)
  * `_run_bulk`        — per-record savepoint -> partial success (writes)
  * `_bulk_read`       — per-record read -> the SAME partial-success envelope (reads)
  * `_list_ok`         — the ONE list envelope every entity emits
  * `_page`            — the ONE limit/offset clamp every list endpoint calls
  * `_read_list`       — parse a JSON-list request arg
  * `normalise_partner_response` — after_request gateway-error normaliser
  * `resolve_lead`     — the ONE grain-scoped lead resolver every entity API calls
  * `EXTERNAL_ID_FIELD` + `stamp_external_id` — the caller's own label (never an address)
"""
import contextlib
import functools
import hashlib
import time

import frappe
from frappe import _
from frappe.model import child_table_fields, default_fields, optional_fields
from frappe.utils import add_to_date, cint, get_datetime, now_datetime

from tatva_connect import automation
from tatva_connect.whatsapp.phone import to_e164

# -- identity ----------------------------------------------------------------
# EVERY record is addressed by `name` — the primary key of its underlying table (CRM Lead,
# CRM Task, CRM Call Log, File). A create returns that name; the caller stores it and uses it
# for every subsequent read, update and delete. That is the ONLY address.
#
# `external_id` is the caller's own LABEL for the record: one column name across every entity,
# free-form, optional, stored and echoed back, never interpreted. It does NOT identify a record
# and does NOT deduplicate — nothing is ever resolved by it.
#
# Retry safety is the Idempotency-Key header (below), and nothing else. The two are unrelated.
EXTERNAL_ID_FIELD = "custom_external_id"

# The ONE action vocabulary every endpoint reports. No entity invents its own verb.
ACTION_CREATED = "created"
ACTION_UPDATED = "updated"
ACTION_FETCHED = "fetched"
ACTION_DELETED = "deleted"


def stamp_external_id(doctype, name, external_id):
	"""Store the caller's label on a row. No-op when the caller sent none (it is optional)."""
	if not external_id:
		return
	frappe.db.set_value(doctype, name, EXTERNAL_ID_FIELD, external_id, update_modified=False)

# Field behavior (Google AIP-203). The one reserved set every partner endpoint shares: a partner
# may never send these (Frappe sets the audit and identity stamps, the Assignment Rule sets the
# owner). OUTPUT_ONLY means discoverable in a schema but ignored on write. Sourced from Frappe's
# own field lists so new standard fields are covered automatically, plus the domain field lead_owner.
RESERVED_FIELDS = frozenset(default_fields) | frozenset(optional_fields) | frozenset(
	child_table_fields
) | {"lead_owner"}

BEHAVIOR_REQUIRED = "REQUIRED"
BEHAVIOR_OPTIONAL = "OPTIONAL"
BEHAVIOR_OUTPUT_ONLY = "OUTPUT_ONLY"


def is_writable(fieldname):
	"""True if a partner may SEND this field (not a reserved audit/system/assignment field)."""
	return fieldname not in RESERVED_FIELDS


def field_behavior(fieldname, required=False):
	"""The AIP-203 field_behavior for a partner-facing descriptor: reserved is OUTPUT_ONLY,
	otherwise REQUIRED or OPTIONAL. One vocabulary across every endpoint's schema."""
	if fieldname in RESERVED_FIELDS:
		return BEHAVIOR_OUTPUT_ONLY
	return BEHAVIOR_REQUIRED if required else BEHAVIOR_OPTIONAL


def field_descriptor(fieldname, label, fieldtype, required=False, options=None, allowed_values=None):
	"""The one partner-facing field descriptor shared by every endpoint schema (lead, activity, ...).
	A reserved field is OUTPUT_ONLY and never required; options apply only to Link and Select."""
	writable = is_writable(fieldname)
	d = {
		"fieldname": fieldname,
		"label": label,
		"type": fieldtype,
		"behavior": field_behavior(fieldname, required),
		"required": bool(required) and writable,
		"options": options if fieldtype in ("Link", "Select") else None,
	}
	if allowed_values:
		d["allowed_values"] = allowed_values
	return d


@contextlib.contextmanager
def trusted_permissions():
	"""Run a block with Frappe permission checks bypassed, for the partner API ONLY.

	The partner is already authorized by the mapping and grain gate (_resolve_caller + resolve_lead),
	and is a role-less user who fails every native role check by design, so a shared engine brain
	(the activity brain, ...) must run trusted here. This is the SAME posture the lead and call
	endpoints take with ignore_permissions=True on their writes; it only ever runs inside a partner
	endpoint that has already gated the caller. The flag is server-set (never from the request body),
	and resets on exit."""
	prev = frappe.flags.ignore_permissions
	frappe.flags.ignore_permissions = True  # authz-ok: server-set only, never from the request; entered post-gate
	try:
		yield
	finally:
		frappe.flags.ignore_permissions = prev

# Config ---------------------------------------------------------------------
# Every numeric knob lives on the `CRM Partner API Settings` Single, read FRESH each
# request via _cfg(). DEFAULTS is the blank-field fallback ONLY — never live policy.
# 0-rules (applied centrally in _cfg, never at call sites):
#   rate/burst fields  -> 0 = UNLIMITED for that dimension
#   cap fields         -> 0 = REJECT (footgun), blank = the DEFAULT below
_SETTINGS = "CRM Partner API Settings"
_RATE_ENFORCEMENT = "Partner::RateLimit::enforcement"

DEFAULTS = {
	"window_seconds": 60,
	"per_token_rate": 120,
	"per_token_burst": 240,
	"global_rate": 300,
	"global_burst": 600,
	"bulk_max_records": 100,
	"list_max_page": 200,
	"list_default_page": 20,
	"file_download_timeout_seconds": 30,
	"file_download_max_mb": 25,
	# Volume dimension — rows/window, read + write, per-token + global (records global = 5x
	# per-token). Single + bulk both charge their row count here.
	"records_window_seconds": 86400,
	"per_token_read_records": 10000,
	"global_read_records": 50000,
	"per_token_write_records": 10000,
	"global_write_records": 50000,
	# Idempotency (opt-in write-dedup): how long a stored key/response is honoured for replay.
	"idempotency_window_hours": 24,
}
# These treat 0 as "unlimited"; every other numeric field is a cap where 0 falls back to its DEFAULT.
_UNLIMITED_WHEN_ZERO = (
	"per_token_rate", "per_token_burst", "global_rate", "global_burst",
	"per_token_read_records", "global_read_records",
	"per_token_write_records", "global_write_records",
)


def _cfg():
	"""The partner-API numeric config, read FRESH each request (a Single is one cheap
	row read; like is_enabled, an edit applies on the very next call — NO cache, no
	cache-clear hook). Blank -> DEFAULTS; then the 0-rules: rate/burst 0 stays 0
	(= unlimited), a cap field of 0 is rejected back to its DEFAULT. Any read error
	-> the DEFAULTS (fail-open)."""
	try:
		row = frappe.db.get_singles_dict(_SETTINGS) or {}
	except Exception:
		frappe.log_error(title="Partner API _cfg read failed")
		return dict(DEFAULTS)
	cfg = {}
	for field, default in DEFAULTS.items():
		val = row.get(field)
		val = default if val in (None, "") else int(val)
		if field not in _UNLIMITED_WHEN_ZERO and val == 0:
			val = default          # cap field: 0 is a footgun -> fall back to the default
		cfg[field] = val
	return cfg


# -- caller resolution -------------------------------------------------------

def _norm_phone(raw):
	"""E.164 normaliser — one brain: delegates to whatsapp.phone.to_e164, keeping the falsy
	passthrough (None/'' unchanged) and the str() coercion (a JSON-number mobile_no still
	normalises) of the original duplicated isdigit/+91 algorithm."""
	return to_e164(str(raw)) if raw else raw


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
	roles = frappe.get_roles(user)
	is_sysmgr = "System Manager" in roles
	# Defense-in-depth (2nd independent gate): an EXTERNAL partner must carry the marker
	# `Partner API User` role — a grant-nothing role (no DocPerm), so it can't re-open
	# /api/resource. The mapping alone is not enough; a user without the role is refused
	# even if a mapping row exists. (System Manager = trusted internal caller, exempt.)
	if mp and not is_sysmgr and "Partner API User" not in roles:
		frappe.throw(_("Not authorised: {0} lacks the Partner API User role").format(user), frappe.PermissionError)
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


def _page(data):
	"""(limit, offset) for a list request, clamped to the configured page ceiling. The ONE
	pagination brain every list endpoint calls — no module-local clamping."""
	cfg = _cfg()
	limit = min(cint(data.get("limit")) or cfg["list_default_page"], cfg["list_max_page"])
	offset = cint(data.get("offset") or data.get("limit_start"))
	return limit, offset


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
	return True  # denial sentinel for throttle guards; body already set, so guards bare-`return`


# -- error mapping + rate limit ----------------------------------------------
# Partners get the unified error contract, never a raw Frappe traceback. The
# exception type maps to a stable code + HTTP status; the message is the throw()'s
# own text (we author those — safe), and anything unexpected is logged server-side
# and returned generically so internals never leak.
_ERROR_MAP = {
	frappe.PermissionError: ("forbidden", 403),
	frappe.DoesNotExistError: ("not_found", 404),
	frappe.ValidationError: ("validation_error", 400),
	# Python builtins (no frappe equivalent) — Frappe's field coercion raises these on bad input
	# (e.g. a malformed date); the caller's fault -> 400, not an opaque 500.
	ValueError: ("validation_error", 400),
	TypeError: ("validation_error", 400),
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
	frappe.log_error(title=f"Partner API error: {fn_name}")
	return "server_error", 500, _("Something went wrong. Please try again or contact support."), None


# A token bucket per key, entirely in Redis and ATOMIC (no read-then-write race in
# Python). HASH {tokens, last_refill}; refill rate/window tokens per elapsed second,
# capped at burst; consume `cost` only if the bucket can pay. ARGV: rate window burst
# cost now. Returns {allowed, retry_after, remaining}. A rate of 0 means UNLIMITED for
# this dimension (the limiter short-circuits to allow before calling the script).
_RL_LUA = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local burst = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local now = tonumber(ARGV[5])
local refill = rate / window
local data = redis.call('HMGET', key, 'tokens', 'last_refill')
local tokens = tonumber(data[1])
local last = tonumber(data[2])
if tokens == nil then tokens = burst; last = now end
local elapsed = now - last
if elapsed > 0 then
  tokens = math.min(burst, tokens + elapsed * refill)
  last = now
end
local allowed = 0
local retry_after = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
else
  local deficit = cost - tokens
  retry_after = math.ceil(deficit / refill)
end
redis.call('HMSET', key, 'tokens', tokens, 'last_refill', last)
redis.call('EXPIRE', key, math.ceil(window * 2))
return {allowed, retry_after, math.floor(tokens)}
"""
_RL_SHA = None


def _run_bucket(name, rate, burst, window, cost):
	"""Consume `cost` from the Redis token bucket `name`. rate 0 => unlimited (skip the
	script). Atomic via EVALSHA (fallback EVAL on NOSCRIPT). Returns
	(allowed, retry_after, remaining)."""
	global _RL_SHA
	if rate <= 0:
		return True, 0, burst
	import redis as _redis

	key = frappe.cache.make_key(f"partner_rl:{name}")
	args = [rate, window, burst, cost, int(time.time())]
	try:
		if _RL_SHA is None:
			_RL_SHA = frappe.cache.script_load(_RL_LUA)
		res = frappe.cache.evalsha(_RL_SHA, 1, key, *args)
	except _redis.exceptions.NoScriptError:
		_RL_SHA = frappe.cache.script_load(_RL_LUA)
		res = frappe.cache.evalsha(_RL_SHA, 1, key, *args)
	allowed, retry_after, remaining = int(res[0]), int(res[1]), int(res[2])
	return bool(allowed), retry_after, remaining


def _bucket_pair(mapping, cost, gname, grate, gburst, tname, trate, tburst, window):
	"""Charge `cost` against a (global, per-token) token-bucket pair — throttled if EITHER denies.
	The ONE brain both the rate (calls) and volume (rows) dimensions run through. Returns None
	(exempt: no mapping) or (retry_after | None, per_token_remaining). Fail-open: ANY Redis/Lua
	error is logged and the request is ALLOWED (returns None). The per-token key is the session
	user — each partner is one User (mapping name == partner_user), so this is the per-partner bucket."""
	if not mapping:
		return None
	try:
		g_ok, g_retry, _g = _run_bucket(gname, grate, gburst, window, cost)
		t_ok, t_retry, t_rem = _run_bucket(tname, trate, tburst, window, cost)
	except Exception:
		frappe.log_error(title="Partner API limiter failed (allowed)")
		return None
	if g_ok and t_ok:
		return None, t_rem
	# Denied: report the longer of the two waits.
	return max(g_retry if not g_ok else 0, t_retry if not t_ok else 0), t_rem


def _rate_check(cost, mapping):
	"""RATE dimension — `cost` calls into the global + per-token call buckets (window_seconds)."""
	cfg = _cfg()
	window = cfg["window_seconds"] or DEFAULTS["window_seconds"]
	return _bucket_pair(
		mapping, cost,
		"global", cfg["global_rate"], cfg["global_burst"],
		f"tok:{frappe.session.user}", cfg["per_token_rate"], cfg["per_token_burst"], window,
	)


def _volume_check(rows, direction, mapping):
	"""VOLUME dimension — `rows` into the read|write daily buckets (global + per-token). Burst =
	the ceiling, so a partner may spend a whole day's budget in one bulk load. `direction` is
	'read' or 'write'; single + bulk endpoints both meter their row count here."""
	cfg = _cfg()
	window = cfg["records_window_seconds"] or DEFAULTS["records_window_seconds"]
	g = cfg[f"global_{direction}_records"]
	t = cfg[f"per_token_{direction}_records"]
	return _bucket_pair(
		mapping, rows,
		f"vol:{direction}:global", g, g,
		f"vol:{direction}:tok:{frappe.session.user}", t, t, window,
	)


def _throttle_response(check, mapping, reason=None):
	"""Given a `_bucket_pair` result, set the RateLimit-* headers + return the 429 `_fail` response
	when denied, else None. One place both dimensions funnel their throttle response through."""
	if check is None:
		return None
	retry_after, remaining = check
	if retry_after is None:
		return None
	_ratelimit_headers(mapping, remaining=remaining, retry_after=retry_after)
	return _fail(
		"rate_limited",
		_("{0}. Retry in {1}s.").format(reason or _("Rate limit exceeded"), retry_after),
		429, retry_after=retry_after,
	)


def _direction():
	"""Single-endpoint volume direction: read on a GET, write otherwise. (Bulk endpoints pass
	their own direction explicitly — lead_get_bulk is a READ over POST, so method is unreliable there.)"""
	req = _request()
	method = ((req.method if req is not None else "GET") or "GET").upper()
	return "read" if method == "GET" else "write"


def _meter_volume(rows, direction):
	"""Charge `rows` against the volume buckets (only when the enforcement switch is ON). Returns a
	429 `_fail` response if the daily budget is exhausted, else None. Exempt callers pass. Bulk
	endpoints call this with their exact row count (writes via _run_bulk, reads inline)."""
	if not automation.is_enabled(_RATE_ENFORCEMENT):
		return None
	_u, mapping, _s = _resolve_caller()
	return _throttle_response(
		_volume_check(rows, direction, mapping), mapping,
		reason=_("Daily {0} record quota exceeded").format(direction),
	)


def _ratelimit_headers(mapping, remaining=None, retry_after=None):
	"""IETF RateLimit-* headers on every partner response (+ Retry-After on a 429), set on
	frappe.local.response_headers (app.py merges these into the final response). The limit
	is the per-token budget (the partner's own ceiling); sysmgr/no-mapping callers are
	exempt and get none. Never raises (header decoration must not break a response)."""
	if not mapping:
		return
	try:
		cfg = _cfg()
		hdrs = frappe.local.response_headers
		hdrs["RateLimit-Limit"] = str(cfg["per_token_rate"])
		if remaining is not None:
			hdrs["RateLimit-Remaining"] = str(max(remaining, 0))
		hdrs["RateLimit-Reset"] = str(cfg["window_seconds"])
		if retry_after is not None:
			hdrs["Retry-After"] = str(retry_after)
	except Exception:
		frappe.log_error(title="Partner API ratelimit headers failed (ignored)")


# -- idempotency (opt-in): make partner WRITE retries safe -------------------
# A client MAY send an Idempotency-Key on a POST/PUT/DELETE. First sight -> we claim it (the doc-name
# PK insert IS the lock), run the endpoint, store the response. A retry with the SAME key + args ->
# replay the stored response (no duplicate). Same key + DIFFERENT args -> 422. Key still in flight ->
# 409. No key -> behaves exactly as before (fully opt-in). Only 2xx responses are cached.
_IDEM_DT = "CRM Partner API Idempotency"
_IDEM_HEADER = "Idempotency-Key"
_IDEM_CLEANUP = "Partner::Idempotency::cleanup"
_IDEM_STALE_SECONDS = 60  # a 'pending' claim older than this = a crashed run -> reclaimable


def _request():
	"""The live werkzeug request, or None when there is no HTTP context.

	`frappe.request` is a Local PROXY: it is NEVER None, and touching an attribute on it raises
	RuntimeError("object is not bound") when no request is bound — a test, the bench console, a
	background job, the scheduler. A bare `getattr(frappe, "request", None)` therefore hands back a
	live-looking proxy that detonates on first use, which surfaced as an opaque 500 from every
	endpoint called outside HTTP. This is the ONE request accessor; every introspecting helper below
	goes through it so a non-HTTP caller degrades to its documented default instead of crashing."""
	req = getattr(frappe, "request", None)
	if req is None:
		return None
	try:
		req.method  # force the proxy to resolve: an unbound Local raises HERE, not on the getattr
	except RuntimeError:
		return None  # no bound request — the callers' defaults below apply (silence is the contract)
	return req


def _idem_key():
	"""The client's key: the Idempotency-Key header first, then a body arg fallback."""
	req = _request()
	hdr = req.headers.get(_IDEM_HEADER) if req is not None else None
	return (hdr or frappe.form_dict.get("idempotency_key") or "").strip()


def _is_write():
	"""Writes carry idempotency; GETs are already idempotent."""
	req = _request()
	m = ((req.method if req is not None else "GET") or "GET").upper()
	return m in ("POST", "PUT", "DELETE")


def _idem_name(user, key):
	return hashlib.sha256(f"{user}:{key}".encode()).hexdigest()  # not SQL — a deterministic doc name


def _fingerprint(fn_name):
	"""Stable hash of the logical request (endpoint + request args, minus framework/idempotency noise),
	so a retry with the same body replays and a reuse with a different body is rejected."""
	data = {k: v for k, v in (frappe.form_dict or {}).items()
	        if k not in ("cmd", "csrf_token", "idempotency_key")}
	return hashlib.sha256((fn_name + "|" + frappe.as_json(data)).encode()).hexdigest()  # not SQL — a fingerprint


def _idempotency_begin(user, key, fn_name):
	"""Claim the key, or short-circuit. Returns ('run', name) | ('replay', None) | ('conflict', None).
	On 'replay' the stored response is set; on 'conflict' a 409/422 _fail is set — caller just returns."""
	name = _idem_name(user, key)
	fp = _fingerprint(fn_name)
	row = frappe.db.get_value(
		_IDEM_DT, name, ["state", "request_fingerprint", "response_body", "creation"], as_dict=True
	)
	if row:
		if row.state == "pending":
			if (now_datetime() - get_datetime(row.creation)).total_seconds() <= _IDEM_STALE_SECONDS:
				_fail("conflict", _("A request with this Idempotency-Key is already in progress."), 409)
				return "conflict", None
			# stale claim (a run that crashed mid-flight) -> reclaim it
			frappe.db.set_value(_IDEM_DT, name, {"request_fingerprint": fp, "creation": now_datetime()})
			frappe.db.commit()
			return "run", name
		if row.request_fingerprint != fp:
			_fail("conflict", _("Idempotency-Key reused with different parameters."), 422)
			return "conflict", None
		frappe.local.response.update(frappe.parse_json(row.response_body or "{}"))  # replay
		return "replay", None
	# first sight -> claim (a duplicate insert on the sha256 PK is the concurrency lock)
	try:
		doc = frappe.new_doc(_IDEM_DT)
		doc.name = name
		doc.flags.name_set = True
		doc.update({"idempotency_key": key, "partner": user, "request_fingerprint": fp, "state": "pending"})
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		return "run", name
	except frappe.DuplicateEntryError:
		frappe.db.rollback()
		_fail("conflict", _("A request with this Idempotency-Key is already in progress."), 409)
		return "conflict", None


def _idempotency_store(name):
	"""After the endpoint ran: cache the response for replay — but ONLY a success. A 4xx/5xx (a _fail
	returned without raising, e.g. a 429 from a bulk volume check) releases the claim so a retry re-runs."""
	if frappe.local.response.get("http_status_code", 200) >= 400:
		_idempotency_release(name)
		return
	frappe.db.set_value(
		_IDEM_DT, name,
		{"state": "done", "response_body": frappe.as_json(dict(frappe.local.response)),
		 "status_code": frappe.local.response.get("http_status_code", 200)},
		update_modified=False,
	)


def _idempotency_release(name):
	"""Drop the claim so a retry actually re-runs (we never cache a failed write)."""
	try:
		frappe.db.delete(_IDEM_DT, {"name": name})
	except Exception:
		frappe.log_error(title="Idempotency release failed")


def purge_idempotency_keys():
	"""Daily: drop idempotency records past the retention window (scheduler; gated, ships dormant)."""
	if not automation.is_enabled(_IDEM_CLEANUP):
		return
	cutoff = add_to_date(now_datetime(), hours=-_cfg()["idempotency_window_hours"])
	frappe.db.delete(_IDEM_DT, {"creation": ["<", cutoff]})
	frappe.db.commit()


def _api(fn=None, *, bulk=False):
	"""Wrap an endpoint: enforce the rate + volume limits (when the switch is ON), run it, and
	emit the unified contract on any failure (never a traceback). RATE = 1 call per request.
	VOLUME = 1 row for a single endpoint (read on GET, write otherwise); a bulk endpoint
	(`@_api(bulk=True)`) charges its own row count via _run_bulk (writes) or _meter_volume (reads).
	Sysmgr/no-mapping callers are exempt. The endpoint writes its body via _ok() and returns None."""
	if fn is None:
		return functools.partial(_api, bulk=bulk)

	@functools.wraps(fn)
	def wrapper(*args, **kwargs):
		idem = None
		try:
			key = _idem_key()
			if key and _is_write():
				action, idem = _idempotency_begin(frappe.session.user, key, fn.__name__)
				if action != "run":
					return  # replay / conflict response already set
			mapping = None
			remaining = None
			if automation.is_enabled(_RATE_ENFORCEMENT):
				_user, mapping, _is_sysmgr = _resolve_caller()
				rate = _rate_check(1, mapping)
				denied = _throttle_response(rate, mapping)
				if denied:
					if idem:
						_idempotency_release(idem)
					return
				remaining = rate[1] if rate else None
				if not bulk:
					denied = _meter_volume(1, _direction())
					if denied:
						if idem:
							_idempotency_release(idem)
						return
			result = fn(*args, **kwargs)
			if mapping is not None:
				_ratelimit_headers(mapping, remaining=remaining)
			if idem:
				_idempotency_store(idem)
			return result
		except Exception as e:
			code, http, message, fields = _classify(e, fn.__name__)
			if fields:
				_fail(code, message, http, fields=fields)
			else:
				_fail(code, message, http)
			if idem:
				_idempotency_release(idem)
	return wrapper


def _bulk_error(i, e, fn_name):
	"""One failed record -> its entry in a bulk `results` array. Shared by the write and read
	lanes so a per-record failure looks IDENTICAL whichever bulk endpoint produced it."""
	code, _http, message, fields = _classify(e, fn_name)
	# the throw populated message_log -> clear it so build_response doesn't
	# leak `_server_messages` into the (otherwise clean) bulk envelope.
	frappe.clear_messages()
	frappe.local.message_log = []
	err = {"code": code, "message": message}
	if fields:
		err["fields"] = fields
	return {"index": i, "status": "error", "error": err}


def _bulk_guard(items, direction):
	"""Shared preamble for both bulk lanes: assert a JSON array, enforce the per-call ceiling, and
	charge VOLUME = len(items) rows in `direction`. Returns the 429 `_fail` sentinel when the daily
	budget is exhausted (the caller bare-`return`s), else None. The 1-call RATE cost was already
	charged by @_api(bulk=True)."""
	if not isinstance(items, list):
		frappe.throw(_("Expected a JSON array"))
	bulk_max = _cfg()["bulk_max_records"]
	if len(items) > bulk_max:
		frappe.throw(_("Max {0} records per call; received {1}. Page the rest.").format(bulk_max, len(items)))
	return _meter_volume(len(items), direction)


def _run_bulk(items, fn):
	"""WRITE lane. Run `fn(index, item)` per record in its own savepoint -> partial success. A failing
	record is rolled back and reported; the rest still commit."""
	if _bulk_guard(items, "write"):
		return
	results, ok = [], 0
	for i, item in enumerate(items):
		sp = f"tc_bulk_{i}"
		frappe.db.savepoint(sp)
		try:
			results.append(fn(i, item))
			ok += 1
		except Exception as e:
			frappe.db.rollback(save_point=sp)
			results.append(_bulk_error(i, e, "bulk"))
	_ok(summary={"total": len(items), "succeeded": ok, "failed": len(items) - ok}, results=results)


def _bulk_read(names, load):
	"""READ lane. Load each record by `name` via `load(name)` -> the SAME partial-success envelope the
	write lane emits ({total, succeeded, failed} + input-ordered results). Results are input-ordered:
	results[i] is the i-th requested name, found or not. Charges the true row count as READ volume —
	a bulk read drains the read budget exactly like N single gets."""
	if _bulk_guard(names, "read"):
		return
	results, ok = [], 0
	for i, name in enumerate(names):
		try:
			results.append({"index": i, "status": "success", "action": ACTION_FETCHED, "data": load(name)})
			ok += 1
		except Exception as e:
			results.append(_bulk_error(i, e, "bulk_read"))
	_ok(summary={"total": len(names), "succeeded": ok, "failed": len(names) - ok}, results=results)


def _list_ok(collection, rows, total, offset, limit):
	"""The ONE list envelope every entity emits: {total, count, offset, limit, has_more, <collection>}.
	`collection` is the entity's plural key (leads / activities / files / calls)."""
	_ok(action=ACTION_FETCHED, data={
		"total": total, "count": len(rows), "offset": offset, "limit": limit,
		"has_more": (offset + len(rows)) < total, collection: rows,
	})


# -- discovery ---------------------------------------------------------------

_ADDRESSING = (
	"Every record is addressed by `name` — the primary key of its underlying table, returned when the "
	"record is created. It is stored by the caller and is the only address the API accepts. "
	"`external_id` is a label of the caller's own choosing: it is stored and echoed back on every read, "
	"is never interpreted, and is never used to locate a record. Retries are made safe with the "
	"Idempotency-Key header, not with any identifier in the body."
)


def _schema_ok(entity, dedup, fields=None, **extra):
	"""The ONE discovery envelope every `*_schema` endpoint emits. `dedup` states in the caller's own
	language how this entity's uniqueness is decided — always by OUR logic, never by an `external_id`."""
	cfg = _cfg()
	data = {
		"entity": entity,
		"identity": {"addressed_by": "name", "note": _ADDRESSING},
		"dedup": dedup,
		"bulk": {"max_per_call": cfg["bulk_max_records"],
		         "list_page_max": cfg["list_max_page"],
		         "list_page_default": cfg["list_default_page"]},
	}
	if fields is not None:
		data["fields"] = fields
	data.update(extra)
	_ok(action=ACTION_FETCHED, data=data)


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
		body = frappe.parse_json(response.get_data(as_text=True) or "{}")
		if not isinstance(body, dict):
			body = {}
		if isinstance(body, dict) and body.get("status") == "error":
			return  # already our contract (an _api/_fail response) — leave it
		code, http, message = _normalise_partner_error(
			request, response.status_code, (body or {}).get("exc_type")
		)
		response.status_code = http
		response.set_data(frappe.as_json({"status": "error", "error": {"code": code, "message": _(message)}}))
		response.headers["Content-Type"] = "application/json"
	except Exception:
		frappe.log_error(title="normalise_partner_response failed")
