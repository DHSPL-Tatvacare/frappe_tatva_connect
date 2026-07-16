# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Drive the async bulk-job tier end-to-end through the real endpoints, exactly as a partner would.

Submit a large lead-create FILE (multipart, so no base64 inflation and no held web worker), then poll
the job to a terminal state and page its per-record results. Nothing here imports the app: it addresses
the job by the id `create` hands back and reads outcomes out of the published envelope.

Records are synthetic but valid — a distinctive synthetic mobile range and name prefix so every lead is
identifiable and purgeable — and padded with an ignored key to reach the target size while staying under
the file record cap (a 50 MB file is ~45k records of ~1 KB, not millions of tiny ones).

    python -m tatva_connect.tests.live.load.async_bulk anaya --mb 25
    python -m tatva_connect.tests.live.load.async_bulk anaya tatvapractice --mb 50
"""
import argparse
import json
import time

from tatva_connect.tests.live.load.client import METHOD, Partner
from tatva_connect.tests.live.load.config import ACCOUNTS, BASE_URL, SITE_HOST, partner_token
from tatva_connect.tests.live.load.log import Logger

NAME_PREFIX = "AsyncLoad"
# +91 61 9X ...: a distinctive synthetic 10-digit range (61-90/61-91) that will not collide with real data.
_PREFIX = {"anaya": "6190", "tatvapractice": "6191"}
# A valid program for each grain — the create brain requires it on this key (custom_current_program).
_PROGRAM = {"anaya": "Nivolumab", "tatvapractice": "FieldSales"}
_TERMINAL = ("JobComplete", "Failed", "Aborted")
_MAX_RECORDS = 45000  # stay under the 50k file record cap


def _mobile(account, i):
	return f"+91{_PREFIX[account]}{i:06d}"  # 4 + 6 = 10 digits


def build_jsonl(account, target_bytes):
	"""A JSONL body of ~target_bytes: N valid lead-create records (N capped under the record cap), each
	padded with an ignored `pad` key so the line reaches the size the target/N budget calls for."""
	n = min(_MAX_RECORDS, max(1, target_bytes // 512))
	per_line = max(80, target_bytes // n)
	out = []
	for i in range(n):
		rec = {"mobile_no": _mobile(account, i), "first_name": f"{NAME_PREFIX}{account[:2].upper()}{i}",
		       "custom_current_program": _PROGRAM[account]}
		pad = per_line - len(json.dumps(rec)) - 11  # 11 ≈ the "pad":"" overhead
		if pad > 0:
			rec["pad"] = "x" * pad
		out.append(json.dumps(rec))
	body = "\n".join(out).encode()
	return body, n


def submit_file(api, raw, operation="lead_create", fmt="jsonl"):
	"""Submit the batch as a multipart upload (a real file upload; no Idempotency-Key, so no base64
	requirement). Returns the parsed 202 body."""
	url = f"{api.base}{METHOD}.partner_bulk_job.create"
	headers = {"Host": api.host, "Authorization": api.headers["Authorization"]}
	resp = api.session.post(url, headers=headers, data={"operation": operation, "format": fmt},
	                        files={"file": (f"load.{fmt}", raw, "application/octet-stream")}, timeout=600)
	resp.raise_for_status()
	body = resp.json()
	if body.get("status") != "success":
		raise SystemExit(f"submit rejected: {json.dumps(body.get('error') or body)[:300]}")
	return body["data"]


def poll(api, job_id, log, interval=3, timeout=1800):
	"""Poll get() until a terminal state (or timeout). Returns the final job data dict."""
	t0 = time.time()
	while time.time() - t0 < timeout:
		call, body = api.get("partner_bulk_job", "get", {"job_id": job_id})
		if not call.ok:
			raise SystemExit(f"get failed: {call.code} {call.message}")
		d = body["data"]
		log.progress("drain", d.get("processed", 0), d.get("total", 0) or 1,
		             f"{d['status']}  {d.get('succeeded', 0)} ok / {d.get('failed', 0)} failed")
		if d["status"] in _TERMINAL:
			return d
		time.sleep(interval)
	raise SystemExit(f"job {job_id} did not finish within {timeout}s")


def sample_results(api, job_id, limit=5):
	"""First page of per-record results — proof the envelope reads and outcomes are addressable."""
	call, body = api.get("partner_bulk_job", "results", {"job_id": job_id, "limit": limit, "offset": 0})
	if not call.ok:
		return {"error": f"{call.code} {call.message}"}
	d = body.get("data") or {}
	return {"total": d.get("total"), "has_more": d.get("has_more"), "sample": d.get("results")}


def run_async(account, target_mb, base=None, host=None, tokens=None, logger=None):
	log = logger or Logger()
	token, grain = partner_token(account, tokens)
	api = Partner(token, base=base, host=host, logger=log, timeout=600)

	raw, n = build_jsonl(account, target_mb * 1_000_000)  # decimal MB: keeps a 50 under the 50 MiB server cap
	log.line(f"[{account}] grain={grain}  ASYNC file job: {n} records, {len(raw) / 1e6:.1f} MB jsonl", "bold")
	log.line(f"[{account}] target {api.base}  (Host: {api.host})", "bright_black")

	started = time.time()
	sub = submit_file(api, raw)
	log.line(f"  submitted {sub['job_id']} ({sub['status']}); draining on the partner_bulk worker…",
	         "bright_black")
	final = poll(api, sub["job_id"], log)
	elapsed = round(time.time() - started, 1)
	results = sample_results(api, sub["job_id"])

	rows = [
		("job id", sub["job_id"]), ("final status", final["status"]),
		("records sent", str(n)), ("total (server)", str(final.get("total"))),
		("succeeded", str(final.get("succeeded"))), ("failed", str(final.get("failed"))),
		("results rows", str(results.get("total"))), ("elapsed", f"{elapsed}s"),
	]
	log.summary(f"{account} — async bulk (grain {grain})", rows, note=f"target {api.base}")
	return {"account": account, "grain": grain, "job_id": sub["job_id"], "records": n,
	        "size_mb": round(len(raw) / 1e6, 1), "final": final, "elapsed_s": elapsed, "results": results}


def main():
	ap = argparse.ArgumentParser(description="drive the async bulk-job tier end-to-end via the endpoints")
	ap.add_argument("accounts", nargs="*", default=list(ACCOUNTS))
	ap.add_argument("--mb", type=int, default=25, help="approx JSONL file size to submit (MB)")
	ap.add_argument("--base", default=BASE_URL, help="target base URL, e.g. https://one-uat.tatvacare.in")
	ap.add_argument("--site", default=SITE_HOST, help="Host header for the target site")
	ap.add_argument("--tokens", default=None,
	                help="partner tokens file under .creds (e.g. partner-api-tokens.uat.json)")
	ap.add_argument("--verbose", action="store_true", help="stream every call")
	args = ap.parse_args()

	logger = Logger(verbose=args.verbose)
	logger.line(f"async bulk jobs -> {args.base}  (Host: {args.site})  tokens: {args.tokens or 'default'}", "bold")
	ok = True
	for account in (args.accounts or ACCOUNTS):
		result = run_async(account, args.mb, base=args.base, host=args.site, tokens=args.tokens, logger=logger)
		f = result["final"]
		if not (f["status"] == "JobComplete" and f.get("failed") == 0 and f.get("succeeded") == result["records"]):
			ok = False
			logger.line(f"[{account}] VALIDATION FAILED: {f['status']} "
			            f"{f.get('succeeded')}/{result['records']} ok, {f.get('failed')} failed", "red")
	raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
	main()
