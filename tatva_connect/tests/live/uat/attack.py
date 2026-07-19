# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""LAYER 2 — fire the cases at a LIVE URL over HTTP and RECORD. No oracle here, by design.

This layer is deliberately dumb: it reads the case list layer 1 exported, resolves a real foreign-owned
target on the live target, fires each case as its persona (API token, or no auth for guest), and writes
one corpus row per case. It never decides whether a response is "right" — layer 3 does that by diffing
against the exported oracle expectation.

Runs anywhere (laptop) — it imports only the frappe-FREE registry + the HTTP engine.

    python -m tatva_connect.tests.live.uat.run --phase attack
"""
import json
import os
import time

from tatva_connect.tests.authz import http_engine
from tatva_connect.tests.authz.registry import endpoints

from . import config

_HERE = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR = os.path.join(_HERE, "..", "reports")
_SPEC_BY_KEY = {e.key: e for e in (*endpoints.GENERIC_ENDPOINTS, *endpoints.APP_ENDPOINTS)}


def _resolve_targets(cfg, doctypes):
	"""A real row per doctype on the LIVE target, found with the admin token. Explicit creds `targets`
	win; anything unresolved is recorded as skip-no-target, never silently dropped."""
	resolved = dict(cfg.get("targets") or {})
	admin = cfg.get("admin_token")
	if not admin:
		return resolved
	eng = http_engine.HttpEngine({"_admin": {"token": admin}}, base=cfg["base"], host=cfg["host"])
	for dt in sorted(d for d in doctypes if d and d not in resolved):
		try:
			code, body = eng.call("_admin", "frappe.client.get_list", http="POST",
			                      params={"doctype": dt, "limit_page_length": 1})
			msg = body.get("message") if isinstance(body, dict) else None
			if code == 200 and msg:
				resolved[dt] = msg[0].get("name") if isinstance(msg[0], dict) else msg[0]
		except Exception:
			continue
	return resolved


def run(cfg=None, expectations_path=None, out_path=None):
	cfg = cfg or config.load()
	expectations_path = expectations_path or os.path.join(_HERE, "expectations.json")
	with open(expectations_path) as fh:
		exported = json.load(fh)
	rows = exported["cases"]

	targets = _resolve_targets(cfg, {r["doctype"] for r in rows})
	eng = http_engine.HttpEngine(config.engine_creds(cfg), base=cfg["base"], host=cfg["host"])
	personas = set(cfg["personas"])

	os.makedirs(REPORT_DIR, exist_ok=True)
	run_id = f"uat-{int(time.time())}"
	out_path = out_path or os.path.join(REPORT_DIR, f"corpus-{run_id}.jsonl")
	fired = skipped = 0
	with open(out_path, "w") as out:
		for r in rows:
			rec = {"run_id": run_id, "case_id": r["case_id"], "persona": r["persona"],
			       "endpoint_key": r["endpoint_key"], "method": r["method"], "action": r["action"],
			       "doctype": r["doctype"], "target": targets.get(r["doctype"])}
			spec = _SPEC_BY_KEY.get(r["endpoint_key"])
			if r["persona"] not in personas:
				rec.update(verdict="SKIPPED", reason="persona not in creds")
			elif not spec:
				rec.update(verdict="SKIPPED", reason="unknown endpoint_key")
			elif r["doctype"] and not rec["target"]:
				rec.update(verdict="SKIPPED", reason="no target on live site")
			else:
				params = endpoints.build_params(spec, r["doctype"], rec["target"])
				try:
					code, body = eng.call(r["persona"], spec.method, http=spec.http, params=params)
					rec.update(status=code, body=_snip(body), verdict="FIRED")
					fired += 1
				except Exception as e:
					rec.update(verdict="ERROR", reason=f"{type(e).__name__}: {str(e)[:120]}")
			if rec["verdict"] != "FIRED":
				skipped += 1
			out.write(json.dumps(rec, default=str) + "\n")
	print(f"[attack] fired={fired} skipped={skipped} -> {out_path}")
	return out_path


def _snip(body):
	"""Keep enough of the response for layer 3 to judge, without hoarding payloads."""
	if not isinstance(body, dict):
		return {"raw": str(body)[:200]}
	out = {k: body[k] for k in ("exc_type", "_server_messages") if k in body}
	msg = body.get("message")
	if isinstance(msg, list):
		out["message"] = msg[:3]
		out["_row_count"] = len(msg)
	elif isinstance(msg, dict):
		out["message"] = {k: msg[k] for k in list(msg)[:12]}
	elif msg is not None:
		out["message"] = str(msg)[:200]
	return out
