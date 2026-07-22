# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Test two: abuse the API on purpose and see whether it degrades or panics.

The paced run (run.py) asks whether the API is correct. This asks whether it is honest under load. It
fires the same slice concurrently, with no pacing and no backoff, and never slows down when told to.
A throttle is recorded rather than obeyed, because the question is what the server does when a client
refuses to behave.

Healthy looks like: a wall of 429s that each carry a Retry-After, no 5xx, and no request that simply
dies. Unhealthy looks like 500s, connection resets, or a 429 with nothing telling the caller when to
come back.

    python -m tatva_connect.tests.partner_api_load.stress anaya --workers 32 --leads 300
"""
import argparse
import json
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

import requests
from requests.adapters import HTTPAdapter

from tatva_connect.tests.partner_api_load import shape
from tatva_connect.tests.partner_api_load.client import Partner
from tatva_connect.tests.partner_api_load.config import (
	ACCOUNTS,
	BASE_URL,
	REPORTS,
	SITE_HOST,
	account_dir,
	field_map,
	partner_token,
)

METHOD = "/api/method/tatva_connect.api"


class Hammer:
	"""One unpaced request. No retry, no backoff, no mercy."""

	def __init__(self, token):
		self.headers = {"Host": SITE_HOST, "Authorization": token, "Content-Type": "application/json"}
		self.session = requests.Session()
		self.session.mount("http://", HTTPAdapter(pool_maxsize=64, max_retries=0))

	def fire(self, module, fn, payload):
		url = f"{BASE_URL}{METHOD}.{module}.{fn}"
		started = time.perf_counter()
		try:
			resp = self.session.post(url, headers=self.headers, json=payload, timeout=120)
		except requests.RequestException as exc:
			return {"endpoint": f"{module}.{fn}", "status": 0, "ms": (time.perf_counter() - started) * 1000,
			        "code": type(exc).__name__, "retry_after": None}
		ms = (time.perf_counter() - started) * 1000

		code, retry_after = None, None
		try:
			body = resp.json()
		except ValueError:
			body = {}
		if body.get("status") != "success":
			err = body.get("error") or {}
			code = err.get("code") or f"http_{resp.status_code}"
			retry_after = err.get("retry_after")
		if resp.status_code == 429 and retry_after is None:
			retry_after = resp.headers.get("Retry-After")

		return {"endpoint": f"{module}.{fn}", "status": resp.status_code, "ms": ms,
		        "code": code, "retry_after": retry_after,
		        "limit": resp.headers.get("X-RateLimit-Limit"),
		        "remaining": resp.headers.get("X-RateLimit-Remaining")}


def _work_items(account, limit, children_spec):
	"""Flatten the slice into independent requests. Every request here stands alone, so the workers
	never wait on each other -- the server is the only thing under contention.

	The bodies are shaped exactly as the paced run shapes them. A body the API would reject on its
	merits would show up as a 4xx and be indistinguishable from load shedding, which would make the
	whole result unreadable.
	"""
	fmap = field_map(account)
	bundles = [json.loads(line) for line in (account_dir(account) / "bundles.jsonl").open()]
	if limit:
		bundles = bundles[:limit]

	dropped = defaultdict(int)
	items = []
	for bundle in bundles:
		body = shape.lead_body(bundle, fmap, children_spec, dropped)
		items.append(("partner", "lead_create", {k: v for k, v in body.items() if not k.startswith("_")}))
	return items


def stress(account, workers, limit):
	token, grain = partner_token(account)
	hammer = Hammer(token)

	call, schema = Partner(token).get("partner", "lead_schema", {})
	if not call.ok:
		raise SystemExit(f"[{account}] lead_schema failed: {call.code} {call.message}")
	items = _work_items(account, limit, schema["data"].get("children") or {})

	print(f"[{account}] grain={grain}  {len(items)} request(s) on {workers} workers, unpaced")
	started = time.time()
	with ThreadPoolExecutor(max_workers=workers) as pool:
		results = list(pool.map(lambda it: hammer.fire(*it), items))
	elapsed = time.time() - started

	statuses = Counter(r["status"] for r in results)
	codes = Counter(r["code"] for r in results if r["code"])
	by_endpoint = defaultdict(list)
	for r in results:
		by_endpoint[r["endpoint"]].append(r["ms"])

	throttled = [r for r in results if r["status"] == 429]
	with_retry_after = [r for r in throttled if r["retry_after"] not in (None, "")]
	server_errors = [r for r in results if 500 <= (r["status"] or 0) < 600]
	dead = [r for r in results if r["status"] == 0]

	rps = len(results) / max(elapsed, 0.001)
	print()
	print(f"===== {account} STRESS =====")
	print(f"  {len(results)} requests in {elapsed:.1f}s = {rps:.0f} req/s offered on {workers} workers")
	print(f"  status codes : {dict(statuses)}")
	if codes:
		print(f"  error codes  : {dict(codes)}")

	latencies = sorted(r["ms"] for r in results)

	def pct(p):
		return round(latencies[min(int(len(latencies) * p / 100), len(latencies) - 1)], 1)

	print(f"  latency ms   : p50={pct(50)} p90={pct(90)} p99={pct(99)} max={round(latencies[-1], 1)}")

	print()
	print("  --- does it degrade honestly? ---")
	print(f"  throttled (429)          : {len(throttled)}")
	print(f"  429s carrying Retry-After: {len(with_retry_after)}/{len(throttled)}"
	      f"{'  <-- OK' if len(with_retry_after) == len(throttled) else '  <-- SOME 429s TELL THE CALLER NOTHING'}")
	print(f"  server errors (5xx)      : {len(server_errors)}"
	      f"{'  <-- OK' if not server_errors else '  <-- THE SERVER PANICKED'}")
	print(f"  dead connections         : {len(dead)}"
	      f"{'  <-- OK' if not dead else '  <-- REQUESTS DIED WITH NO RESPONSE'}")

	healthy = not server_errors and not dead and len(with_retry_after) == len(throttled)
	print()
	print("  VERDICT: the API shed load honestly." if healthy
	      else "  VERDICT: the API did NOT degrade cleanly — see above.")

	REPORTS.mkdir(parents=True, exist_ok=True)
	path = REPORTS / f"{account}.stress.json"
	path.write_text(json.dumps({
		"account": account, "workers": workers, "requests": len(results),
		"elapsed_s": round(elapsed, 1), "offered_rps": round(rps, 1),
		"statuses": {str(k): v for k, v in statuses.items()},
		"error_codes": dict(codes),
		"latency_ms": {"p50": pct(50), "p90": pct(90), "p99": pct(99), "max": round(latencies[-1], 1)},
		"throttled": len(throttled), "throttled_with_retry_after": len(with_retry_after),
		"server_errors": len(server_errors), "dead_connections": len(dead),
		"healthy": healthy,
	}, indent=2) + "\n")
	print(f"  written: {path}")
	return healthy


def main():
	ap = argparse.ArgumentParser(description="unpaced stress test of the partner API")
	ap.add_argument("accounts", nargs="*", default=list(ACCOUNTS))
	ap.add_argument("--workers", type=int, default=32)
	ap.add_argument("--leads", type=int, default=300)
	args = ap.parse_args()
	for account in (args.accounts or ACCOUNTS):
		stress(account, args.workers, args.leads)


if __name__ == "__main__":
	main()
