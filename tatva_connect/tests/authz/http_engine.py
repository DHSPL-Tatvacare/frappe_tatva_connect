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


class HttpEngine:
	def __init__(self, creds, base=DEFAULT_BASE, host=DEFAULT_HOST):
		self.creds = creds
		self.base = base
		self.host = host

	def token(self, persona):
		"""The persona's API token, or None for the guest (unauthenticated) principal."""
		if persona in ("guest", "Guest"):
			return None
		return self.creds[persona]["token"]

	def call(self, persona, path, http="GET", params=None, raw_path=None):
		"""Fire one request AS `persona`. `path` = dotted whitelisted method; `raw_path` overrides for
		/api/resource. Never raises on 4xx/5xx — returns (status_code, body) so the case can judge it."""
		url = f"{self.base}{raw_path}" if raw_path else f"{self.base}/api/method/{path}"
		headers = {"Host": self.host}
		tok = self.token(persona)
		if tok:
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
		return code, body
