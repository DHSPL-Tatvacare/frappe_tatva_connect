# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Two tests. One asks whether the system holds under realistic load; the other whether the ceiling
actually refuses. They are different questions and they need different traffic.

    python -m tatva_connect.tests.loadtest.mixed anaya --mode load
    python -m tatva_connect.tests.loadtest.mixed anaya --mode burst

LOAD — every resource's single endpoint, together, spread the way a partner writes.

  200 lead_create + 200 activity_create + 200 call_create + 200 file_attach, shuffled so the four
  code paths interleave and contend the way they will in production. Every child write picks a lead
  at RANDOM from a pool of many: a partner writes across their whole patient list, not into one
  record. An earlier revision of this file sent all six hundred child writes at ONE lead and measured
  a 46-second row-lock wait — a hotspot the test itself manufactured, and a number that meant nothing.

  Paced just under the burst ceiling so the limiter barely fires. Whatever happens is then the
  system's own behaviour, not a throttle. ANY 5xx is a defect.

BURST — the limiter, tested properly.

  The limiter is a token bucket: it holds `burst` tokens and refills at `rate` per window. Pacing a
  run at three times the rate does NOT saturate it — the bucket refills as fast as it is spent, so it
  never hard-refuses. That is exactly the mistake an earlier revision made, and it reported "the
  limiter did not enforce the ceiling" when the truth was that the test never pressed on it.

  To press on it: fire far more than `burst` requests as fast as the machine allows, with no pacing.
  The bucket empties, and everything after it MUST be refused with a 429 carrying a Retry-After.

The seed ASSERTS. If it cannot get the leads, the task types or the documents it needs, it stops. It
does not quietly run a file test with no files, which is what the earlier revision did.
"""
import argparse
import base64
import json
import random
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

import requests
from requests.adapters import HTTPAdapter

from tatva_connect.tests.loadtest.config import (
	BASE_URL,
	REPORTS,
	SITE_HOST,
	account_dir,
	lsq_creds,
	partner_token,
)
from tatva_connect.tests.loadtest.files import _download, _fresh_urls

METHOD = "/api/method/tatva_connect.api"

BURST = 240  # CRM Partner API Settings.per_token_burst — what the bucket holds


class Pacer:
	def __init__(self, per_minute):
		self.interval = 60.0 / max(per_minute, 1)
		self.lock = threading.Lock()
		self.next_slot = time.monotonic()

	def take(self):
		with self.lock:
			slot = max(self.next_slot, time.monotonic())
			self.next_slot = slot + self.interval
		delay = slot - time.monotonic()
		if delay > 0:
			time.sleep(delay)


class Client:
	def __init__(self, token):
		self.headers = {"Host": SITE_HOST, "Authorization": token, "Content-Type": "application/json"}
		self.session = requests.Session()
		self.session.mount("http://", HTTPAdapter(pool_maxsize=80, max_retries=0))

	def post(self, endpoint, body):
		started = time.perf_counter()
		try:
			resp = self.session.post(f"{BASE_URL}{METHOD}.{endpoint}", headers=self.headers,
			                         json=body, timeout=180)
		except requests.RequestException as exc:
			return {"endpoint": endpoint, "status": 0, "ms": (time.perf_counter() - started) * 1000,
			        "code": type(exc).__name__, "retry_after": None}
		ms = (time.perf_counter() - started) * 1000
		try:
			body_json = resp.json()
		except ValueError:
			body_json = {}
		err = body_json.get("error") or {}
		return {"endpoint": endpoint, "status": resp.status_code, "ms": ms,
		        "code": err.get("code"), "retry_after": err.get("retry_after"),
		        "name": (body_json.get("data") or {}).get("name")}

	def get(self, endpoint, params):
		"""A seed read, honouring a throttle the way the docs tell a partner to."""
		for _ in range(10):
			resp = self.session.get(f"{BASE_URL}{METHOD}.{endpoint}", headers=self.headers,
			                        params=params, timeout=60)
			body = resp.json()
			err = body.get("error") or {}
			if err.get("code") in ("rate_limited", "server_busy"):
				time.sleep((err.get("retry_after") or 2) + 0.5)
				continue
			if body.get("status") != "success":
				raise SystemExit(f"SEED FAILED — {endpoint}: {err.get('code')} {err.get('message')}")
			return body["data"]
		raise SystemExit(f"SEED FAILED — {endpoint}: still throttled after 10 attempts")


def seed(client, account, want_leads, want_docs):
	"""The pool the load writes into. Asserts on every input; a missing one stops the run.

	Leads are minted HERE rather than fished out of lead_list. A grain accumulates leads from every
	run ever done against it, lead_list returns the newest first, and the newest are whatever the last
	test happened to create — so a seed that reads lead_list gets the previous test's leftovers and,
	crucially, leads with no LeadSquared id and therefore no documents. Minting gives a known pool.

	The documents come from the pulled LSQ bundles on disk, which carry the prospect ids the presigned
	urls are minted against.
	"""
	print("  seeding:")

	leads = []
	for i in range(want_leads):
		r = client.post("partner.lead_create", {
			"mobile_no": f"+9196{random.randint(10000000, 99999999)}",
			"first_name": f"Load pool {i}", "custom_vertical": "GoodFlip Care",
			"custom_group": "Anaya", "custom_current_program": "Nivolumab"})
		if r["status"] == 200:
			leads.append(r.get("name"))
		elif r["status"] == 429:
			time.sleep((r.get("retry_after") or 2) + 0.5)
	leads = [n for n in leads if n]
	if len(leads) < want_leads * 0.9:
		raise SystemExit(
			f"SEED FAILED — minted only {len(leads)} of {want_leads} leads. Child writes must SPREAD "
			f"across leads or the test manufactures its own hotspot."
		)
	print(f"    leads minted       : {len(leads)}")

	types = client.get("partner_activity.activity_schema", {"lead": leads[0]})["task_types"]
	if not types:
		raise SystemExit("SEED FAILED — no activity types available for this grain")
	task_types = [t["name"] for t in types]
	print(f"    activity types     : {len(task_types)}")

	docs = []
	if want_docs:
		bundles = account_dir(account) / "bundles.jsonl"
		if not bundles.exists():
			raise SystemExit(f"SEED FAILED — no pulled LSQ data at {bundles}; run pull.py first")
		creds = lsq_creds(account)
		scanned = 0
		for line in bundles.open():
			if len(docs) >= want_docs:
				break
			scanned += 1
			for url in _fresh_urls(creds, json.loads(line)["prospect_id"]):
				content, _err = _download(url["url"])
				if content:
					docs.append({"filename": url["filename"],
					             "b64": base64.b64encode(content).decode(),
					             "kb": round(len(content) / 1024)})
				if len(docs) >= want_docs:
					break
		if len(docs) < want_docs:
			raise SystemExit(
				f"SEED FAILED — {len(docs)} document(s) harvested from {scanned} LSQ lead(s), "
				f"{want_docs} needed. A file test with no files is not a file test."
			)
		print(f"    documents          : {len(docs)}  ({min(d['kb'] for d in docs)}-"
		      f"{max(d['kb'] for d in docs)} KB, from {scanned} LSQ lead(s))")

	return leads, task_types, docs


def _lead_body():
	return {"mobile_no": f"+9197{random.randint(10000000, 99999999)}",
	        "first_name": "Mixed load", "custom_vertical": "GoodFlip Care",
	        "custom_group": "Anaya", "custom_current_program": "Nivolumab"}


def build_mixed(leads, task_types, docs, per_endpoint):
	"""200 of each, every child write on a RANDOM lead — a partner writes across their whole list."""
	work = []
	for _ in range(per_endpoint):
		work.append(("partner.lead_create", _lead_body()))
		work.append(("partner_activity.activity_create",
		             {"lead": random.choice(leads), "task_type": random.choice(task_types),
		              "values": {}}))
		work.append(("partner_call.call_create",
		             {"lead": random.choice(leads), "direction": "Outbound",
		              "from_number": "9000000001", "to_number": "9000000002",
		              "status": "Completed"}))
		if docs:
			d = random.choice(docs)
			work.append(("partner_file.file_attach",
			             {"lead": random.choice(leads), "filename": d["filename"],
			              "file_type": "load-probe", "content_base64": d["b64"]}))
	random.shuffle(work)
	return work


def _percentiles(values):
	s = sorted(values)

	def at(p):
		return round(s[min(int(len(s) * p / 100), len(s) - 1)])

	return at(50), at(95), at(99), round(s[-1])


def _report(account, mode, results, elapsed, extra):
	by_ep = defaultdict(list)
	for r in results:
		by_ep[r["endpoint"]].append(r)

	print(f"\n  {len(results)} requests in {elapsed:.0f}s = {len(results) / elapsed * 60:.0f}/min actual\n")
	print(f"  {'endpoint':<32}{'n':>5}{'200':>6}{'429':>6}{'5xx':>6}"
	      f"{'p50':>8}{'p95':>8}{'p99':>8}{'max':>9}")
	per_ep, total = {}, Counter()
	for ep in sorted(by_ep):
		rows = by_ep[ep]
		ok = sum(1 for r in rows if r["status"] == 200)
		throttled = sum(1 for r in rows if r["status"] == 429)
		fivexx = sum(1 for r in rows if 500 <= (r["status"] or 0) < 600)
		p50, p95, p99, mx = _percentiles([r["ms"] for r in rows])
		total["ok"] += ok
		total["429"] += throttled
		total["5xx"] += fivexx
		per_ep[ep] = {"n": len(rows), "ok": ok, "throttled": throttled, "server_errors": fivexx,
		              "p50": p50, "p95": p95, "p99": p99, "max": mx}
		print(f"  {ep:<32}{len(rows):>5}{ok:>6}{throttled:>6}{fivexx:>6}"
		      f"{p50:>8}{p95:>8}{p99:>8}{mx:>9}")

	throttles = [r for r in results if r["status"] == 429]
	with_ra = [r for r in throttles if r["retry_after"]]
	dead = [r for r in results if r["status"] == 0]
	codes = Counter(r["code"] for r in results if r["code"])

	print()
	print(f"  error codes            : {dict(codes) or 'none'}")
	print(f"  succeeded (200)        : {total['ok']}")
	print(f"  throttled (429)        : {total['429']}")
	if throttles:
		ok_ra = len(with_ra) == len(throttles)
		print(f"  429s with Retry-After  : {len(with_ra)}/{len(throttles)}"
		      f"{'  OK' if ok_ra else '  *** SOME SAY NOTHING ***'}")
	print(f"  server errors (5xx)    : {total['5xx']}"
	      f"{'  OK' if not total['5xx'] else '  *** THE SYSTEM BROKE ***'}")
	print(f"  dead connections       : {len(dead)}{'  OK' if not dead else '  *** DIED ***'}")

	if mode == "load":
		healthy = total["5xx"] == 0 and not dead
		verdict = ("the system carried a realistic mixed load with no failures"
		           if healthy else "the system did NOT hold under a legal load")
	else:
		refused = total["429"] > 0
		honest = not throttles or len(with_ra) == len(throttles)
		healthy = refused and honest and total["5xx"] == 0 and not dead
		verdict = (f"the limiter refused {total['429']} of {len(results)} and told each one when to "
		           f"come back" if healthy else "the limiter did NOT enforce the ceiling")
	print(f"\n  VERDICT: {verdict}")

	REPORTS.mkdir(parents=True, exist_ok=True)
	path = REPORTS / f"{account}.mixed.{mode}.json"
	path.write_text(json.dumps({
		"account": account, "mode": mode, "burst": BURST, **extra,
		"requests": len(results), "elapsed_s": round(elapsed, 1),
		"actual_rpm": round(len(results) / elapsed * 60),
		"per_endpoint": per_ep, "error_codes": dict(codes),
		"succeeded": total["ok"], "throttled": total["429"],
		"throttled_with_retry_after": len(with_ra),
		"server_errors": total["5xx"], "dead": len(dead),
		"healthy": healthy, "verdict": verdict,
	}, indent=2) + "\n")
	print(f"  written: {path}")
	return healthy


def run_load(account, per_endpoint, workers, want_leads, want_docs):
	token, grain = partner_token(account)
	client = Client(token)
	rpm = int(BURST * 0.95)

	print(f"[{account}] {grain}  LOAD — {rpm}/min offered against a burst of {BURST}")
	leads, task_types, docs = seed(client, account, want_leads, want_docs)

	work = build_mixed(leads, task_types, docs, per_endpoint)
	pacer = Pacer(rpm)
	print(f"\n  {len(work)} requests, {per_endpoint} per endpoint, shuffled, "
	      f"child writes spread across {len(leads)} leads, {workers} workers")

	def fire(item):
		pacer.take()
		return client.post(*item)

	started = time.time()
	with ThreadPoolExecutor(max_workers=workers) as pool:
		results = list(pool.map(fire, work))
	return _report(account, "load", results, time.time() - started,
	               {"offered_rpm": rpm, "leads_in_pool": len(leads), "documents": len(docs)})


def run_burst(account, requests_n, workers, want_leads):
	"""No pacing. Empty the bucket and keep pushing — everything past `burst` must be refused."""
	token, grain = partner_token(account)
	client = Client(token)

	print(f"[{account}] {grain}  BURST — {requests_n} requests, UNPACED, against a bucket of {BURST}")
	print(f"  expectation: about {BURST} pass, the remaining ~{requests_n - BURST} are refused with 429")
	leads, task_types, _ = seed(client, account, want_leads, 0)

	work = []
	for _ in range(requests_n // 3):
		work.append(("partner.lead_create", _lead_body()))
		work.append(("partner_activity.activity_create",
		             {"lead": random.choice(leads), "task_type": random.choice(task_types),
		              "values": {}}))
		work.append(("partner_call.call_create",
		             {"lead": random.choice(leads), "direction": "Outbound",
		              "from_number": "9000000001", "to_number": "9000000002",
		              "status": "Completed"}))
	random.shuffle(work)
	print(f"\n  {len(work)} requests, no pacing, {workers} workers")

	started = time.time()
	with ThreadPoolExecutor(max_workers=workers) as pool:
		results = list(pool.map(lambda item: client.post(*item), work))
	return _report(account, "burst", results, time.time() - started,
	               {"offered_rpm": None, "leads_in_pool": len(leads)})


def main():
	ap = argparse.ArgumentParser(description="mixed load, and the limiter under a real burst")
	ap.add_argument("accounts", nargs="*", default=["anaya"])
	ap.add_argument("--mode", choices=("load", "burst"), default="load")
	ap.add_argument("--per-endpoint", type=int, default=200)
	ap.add_argument("--requests", type=int, default=600, help="burst mode only")
	ap.add_argument("--workers", type=int, default=24)
	ap.add_argument("--leads", type=int, default=50, help="the pool child writes spread across")
	ap.add_argument("--files", type=int, default=6)
	args = ap.parse_args()
	for account in args.accounts:
		if args.mode == "load":
			run_load(account, args.per_endpoint, args.workers, args.leads, args.files)
		else:
			run_burst(account, args.requests, max(args.workers, 48), args.leads)


if __name__ == "__main__":
	main()
