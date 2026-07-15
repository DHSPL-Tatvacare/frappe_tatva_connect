"""Thin HTTP client for the gated partner API. LOCAL test harness (un-indexed).

Talks to dev.localhost via the frontend port (8080) with token auth. Every endpoint writes a
top-level {status, data|error} body (not Frappe's {message} wrapper), so we return that dict as-is.
POST/PUT/DELETE send a JSON body (child arrays stay real JSON lists); GET sends a query string.
Token auth bypasses CSRF, so no csrf_token is needed.
"""
import json
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode


class PartnerClient:
    def __init__(self, token, base="http://localhost:8080", host="dev.localhost"):
        self.token = token
        self.base = base
        self.host = host

    def _call(self, module, method, http="GET", params=None):
        """Returns (body_dict, status_code, elapsed_ms). Never raises on a 4xx/5xx: the body
        carries the unified error contract, which is exactly what we want to record."""
        url = f"{self.base}/api/method/tatva_connect.api.{module}.{method}"
        headers = {"Host": self.host, "Authorization": f"token {self.token}"}
        data = None
        if http == "GET":
            if params:
                url += "?" + urlencode(params)
        else:
            data = json.dumps(params or {}).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=http)
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                body, code = r.read().decode(), r.status
        except urllib.error.HTTPError as e:
            body, code = e.read().decode(), e.code
        elapsed = int((time.monotonic() - t0) * 1000)
        try:
            body = json.loads(body)
        except ValueError:
            pass
        return body, code, elapsed

    # -- lead ----------------------------------------------------------------
    def lead_schema(self):
        return self._call("partner", "lead_schema")

    def lead_create(self, payload):
        return self._call("partner", "lead_create", "POST", payload)

    def lead_update(self, payload):
        return self._call("partner", "lead_update", "PUT", payload)

    def lead_get(self, **kw):
        return self._call("partner", "lead_get", "GET", kw)

    def lead_delete(self, name):
        return self._call("partner", "lead_delete", "DELETE", {"name": name})

    # -- activity ------------------------------------------------------------
    def activity_schema(self, **kw):
        return self._call("partner_activity", "activity_schema", "GET", kw)

    def activity_create(self, payload):
        return self._call("partner_activity", "activity_create", "POST", payload)

    # -- file ----------------------------------------------------------------
    def file_attach(self, payload):
        return self._call("partner_file", "file_attach", "POST", payload)

    def file_list(self, **kw):
        return self._call("partner_file", "file_list", "GET", kw)
