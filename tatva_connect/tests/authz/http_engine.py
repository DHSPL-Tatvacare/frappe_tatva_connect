# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""HTTP engine for the authz harness — fire a real request AS a roster persona (token auth).

Layers 2 (endpoint sweep) and 3 (VAPT pins) run over real HTTP on a separate connection, so they use
the COMMITTED seed (generator.seed(commit=True)) + creds.json, the same lifecycle Playwright uses
(README.md section 8). Token auth resolves to the SAME frappe.session.user as a browser login, so
authorization behaves identically, without CSRF friction. One transport, reused by every HTTP case.

`path` is the FULL dotted method, so a case can target ANY installed app or the generic Frappe
endpoints, not just tatva_connect: frappe.client.get, helpdesk.api.doc.get_list_data,
lms.lms.api.get_courses, tatva_connect.api.partner.lead_get, ...
"""
import json
import urllib.error
import urllib.request
from urllib.parse import urlencode

# The in-container gunicorn (the sweep runs IN the bench, so it hits the site by its INTERNAL url, not
# the host port map). test_endpoint_sweep.run() passes base explicitly; this is only the bare-ctor default.
DEFAULT_BASE = "http://localhost:8000"
DEFAULT_HOST = "dev.localhost"


def load_creds(path):
	"""Read generator's creds.json into {persona: {email, token, grain_key, ...}}."""
	with open(path) as fh:
		return {r["persona"]: r for r in json.load(fh)}


class AuthError(RuntimeError):
	"""Login or identity check failed. Deliberately fatal: an unauthenticated request is refused at the
	door, which is indistinguishable from a correctly-blocked attack — so a run MUST stop, never score."""


class ThrottleError(RuntimeError):
	"""The target rate-limited us (429). Same disease as AuthError: a throttled request is refused
	WITHOUT the permission engine ever being consulted, so scoring it as 'denied' would manufacture a
	clean report out of requests that never landed. Stop and slow down instead."""


class HttpEngine:
	def __init__(self, creds, base=DEFAULT_BASE, host=DEFAULT_HOST):
		self.creds = creds
		self.base = base
		self.host = host
		self._sessions = {}  # persona -> sid, populated by login(); one login per persona per run

	def token(self, persona):
		"""The persona's API token, or None for the guest (unauthenticated) principal."""
		if persona in ("guest", "Guest"):
			return None
		return (self.creds.get(persona) or {}).get("token")

	# -- password login (the LIVE/UAT path) --------------------------------------------------------
	# A deployment we do not own has no API tokens for us to mint, so a live run authenticates the way
	# a pen-tester does: POST /api/method/login with usr+pwd, keep the `sid` cookie, send it on every
	# request. Verified against frappe/auth.py on 2026-07-23: a session created through the API carries
	# NO csrf_token, and validate_csrf_token() returns early when the session has none — so an API-login
	# session needs no X-Frappe-CSRF-Token, for reads AND writes. If a deployment ever does enforce it,
	# call() surfaces CSRFTokenError loudly rather than letting it read as a blocked attack.
	def login(self, persona):
		"""Log `persona` in with its password and cache the sid. Returns the sid, or None for guest.

		Called ONCE per persona per run, deliberately: live sites rate-limit and lock accounts on
		repeated logins, so this never retries. A failed login raises — it must never be swallowed and
		counted as 'the attack was blocked' (that is exactly how a run reports all-clear having tested
		nothing)."""
		if persona in ("guest", "Guest"):
			return None
		if persona in self._sessions:
			return self._sessions[persona]
		cred = self.creds.get(persona) or {}
		usr, pwd = cred.get("email") or cred.get("usr"), cred.get("password") or cred.get("pwd")
		if not (usr and pwd):
			raise AuthError(f"{persona}: no username/password in creds (token-only personas cannot log in)")
		code, body, cookies = self._raw("POST", "/api/method/login", {"usr": usr, "pwd": pwd}, sid=None)
		sid = cookies.get("sid")
		if code != 200 or not sid:
			raise AuthError(f"{persona}: login failed (HTTP {code}) — {str(body)[:120]}")
		self._sessions[persona] = sid
		return sid

	def whoami(self, persona):
		"""Who does the live site think we are? The pre-flight: a run must abort unless every persona
		comes back as the user we expect. Without this, a dead credential looks like a perfect defence."""
		_, body = self.call(persona, "frappe.auth.get_logged_user")
		return body.get("message") if isinstance(body, dict) else None

	def authenticate_all(self, personas):
		"""Log in every persona and PROVE each session is the right user. Returns {persona: email}.
		Raises on the first failure — a partially-authenticated run produces meaningless verdicts."""
		out = {}
		for p in personas:
			if p in ("guest", "Guest"):
				out[p] = None  # unauthenticated on purpose; nothing to prove
				continue
			self.login(p)
			who = self.whoami(p)
			expected = (self.creds.get(p) or {}).get("email")
			if not who or (expected and who.lower() != expected.lower()):
				raise AuthError(f"{p}: session identifies as {who!r}, expected {expected!r}")
			out[p] = who
		return out

	def call(self, persona, path, http="GET", params=None, raw_path=None):
		"""Fire one request AS `persona`. `path` = dotted whitelisted method; `raw_path` overrides for
		/api/resource. Never raises on 4xx/5xx — returns (status_code, body) so the case can judge it."""
		url = f"{self.base}{raw_path}" if raw_path else f"{self.base}/api/method/{path}"
		headers = {"Host": self.host}
		tok = self.token(persona)
		sid = self._sessions.get(persona)
		if sid:
			headers["Cookie"] = f"sid={sid}"  # session auth (live/UAT) wins when we hold one
		elif tok:
			headers["Authorization"] = f"token {tok}"
		data = None
		if http == "GET":
			if params:
				url += "?" + urlencode(params)
		else:
			data = json.dumps(params or {}).encode()
			headers["Content-Type"] = "application/json"
		req = urllib.request.Request(url, data=data, headers=headers, method=http)
		try:
			with urllib.request.urlopen(req, timeout=60) as r:
				body, code = r.read().decode(), r.status
		except urllib.error.HTTPError as e:
			body, code = e.read().decode(), e.code
		try:
			body = json.loads(body)
		except ValueError:
			pass
		# Three refusals are NOT permission decisions and must never be scored as "attack blocked":
		# 429 (rate limited), 401 (session dead mid-run), and CSRF. Each means the request never reached
		# the permission engine, so a verdict would be manufactured. Stop the run instead.
		if code == 429:
			raise ThrottleError(f"{persona}: HTTP 429 on {http} {path} — target is rate-limiting. "
			                    "Increase --delay; these responses cannot be judged.")
		if isinstance(body, dict) and body.get("exc_type") == "CSRFTokenError":
			raise AuthError(f"{persona}: CSRFTokenError on {http} {path} — this target enforces CSRF; "
			                "the run would otherwise score these as 'denied'")
		if code == 401 and persona not in ("guest", "Guest"):
			raise AuthError(f"{persona}: HTTP 401 mid-run on {http} {path} — the session died "
			                "(expired or revoked); every later case would score as a false 'denied'")
		return code, body

	def _raw(self, http, path, params, sid=None):
		"""One request that also returns the response cookies — used by login() to capture `sid`."""
		url = f"{self.base}{path}"
		headers = {"Host": self.host, "Content-Type": "application/json"}
		if sid:
			headers["Cookie"] = f"sid={sid}"
		data = json.dumps(params or {}).encode() if http != "GET" else None
		req = urllib.request.Request(url, data=data, headers=headers, method=http)
		try:
			with urllib.request.urlopen(req, timeout=60) as r:
				body, code, raw = r.read().decode(), r.status, r.headers.get_all("Set-Cookie") or []
		except urllib.error.HTTPError as e:
			body, code, raw = e.read().decode(), e.code, e.headers.get_all("Set-Cookie") or []
		cookies = {}
		for c in raw:
			name, _, rest = c.partition("=")
			cookies[name.strip()] = rest.split(";", 1)[0]
		try:
			body = json.loads(body)
		except ValueError:
			pass
		return code, body, cookies
