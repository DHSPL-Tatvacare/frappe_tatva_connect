# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Drive every file endpoint — single and bulk — with real LSQ documents, and prove ClamAV runs.

The file lane is the one part of the partner API the paced load never exercised: the bulk activity
read LSQ uses does not hand back attachment URLs, so no file was ever offered and the virus scanner
never saw a byte.

LSQ will not return an activity's payload and its file URLs in the same read. It presigns a URL for
about thirty minutes, so a URL harvested in an earlier phase is dead by the time a later phase would
use it. The migration loader answers both problems the same way and this does too: the URL is minted
HERE, seconds before the byte is fetched. An expired or deleted object is recorded and skipped — the
subject of this test is our API, not LSQ's S3.

    python -m tatva_connect.tests.partner_api_load.files anaya --leads 20
    python -m tatva_connect.tests.partner_api_load.files anaya --leads 20 --no-clamav
"""
import argparse
import base64
import json
import re
import time
from collections import Counter, defaultdict

import requests

from tatva_connect.tests.partner_api_load.client import Partner
from tatva_connect.tests.partner_api_load.config import REPORTS, lsq_creds, partner_token

# EICAR: the industry-standard harmless string every scanner is required to flag. If ClamAV is live,
# this MUST be rejected — and that rejection is the only proof the scanner is actually in the path.
EICAR = r"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"

URL = re.compile(r"https?://[^\s\"'\\,{}]+", re.I)
DOC_HINT = ("leadsquaredcdn", "amazonaws", "lsq-private-storage", "leadattachment")


def _fresh_urls(creds, prospect_id, timeout=60):
	"""Mint presigned document URLs for ONE lead, seconds before they are used.

	Two sources, both minted now: the activity json (getFileURL presigns custom-field uploads inline)
	and the notes (RetrieveNote carries its own AttachmentURL).
	"""
	auth = {"accessKey": creds["LSQ_ACCESS_KEY"], "secretKey": creds["LSQ_SECRET_KEY"]}
	base = creds["LSQ_HOST"].rstrip("/") + "/v2"
	out = []

	resp = requests.post(f"{base}/ProspectActivity.svc/Retrieve",
	                     params={**auth, "leadId": prospect_id, "getFileURL": "true"},
	                     json={}, timeout=timeout)
	if resp.status_code == 200:
		body = resp.json()
		rows = body.get("ProspectActivities") if isinstance(body, dict) else body
		for act in (rows or []):
			blob = json.dumps(act)
			for url in URL.findall(blob):
				if any(h in url.lower() for h in DOC_HINT):
					out.append({"url": url, "activity": act.get("Id"), "source": "activity"})

	resp = requests.post(f"{base}/LeadManagement.svc/RetrieveNote", params=auth, json={
		"Parameter": {"RelatedId": prospect_id},
		"Paging": {"PageIndex": 1, "PageSize": 50},
	}, timeout=timeout)
	if resp.status_code == 200:
		body = resp.json()
		for note in ((body.get("List") or body.get("Notes") or []) if isinstance(body, dict) else []):
			url = note.get("AttachmentURL") or ""
			if url and any(h in url.lower() for h in DOC_HINT):
				out.append({"url": url, "activity": None, "source": "note",
				            "filename": note.get("AttachmentName")})

	seen, uniq = set(), []
	for row in out:
		path = row["url"].split("?")[0]
		if path in seen:
			continue
		seen.add(path)
		row["path"] = path
		row["filename"] = row.get("filename") or path.rsplit("/", 1)[-1] or "lsq_document"
		uniq.append(row)
	return uniq


def _download(url, timeout=30):
	"""Fetch the bytes. A dead or expired object is a fact about LSQ, not a failure of our API."""
	try:
		resp = requests.get(url, timeout=timeout)
	except requests.RequestException as exc:
		return None, f"transport: {type(exc).__name__}"
	if resp.status_code == 200:
		return resp.content, None
	return None, f"http_{resp.status_code}"


class Suite:
	"""Every file endpoint, single and bulk, timed."""

	def __init__(self, api):
		self.api = api
		self.results = Counter()
		self.failures = []
		self.timings = defaultdict(list)

	def _call(self, module, fn, payload=None, params=None, label=None):
		started = time.perf_counter()
		if params is not None:
			call, body = self.api.get(module, fn, params)
		else:
			call, body = self.api.post(module, fn, payload)
		self.timings[label or fn].append((time.perf_counter() - started) * 1000)
		if call.ok:
			self.results[fn] += 1
		else:
			self.failures.append({"endpoint": fn, "code": call.code, "message": call.message})
		return call, body

	def attach(self, lead, filename, content, file_type=None):
		return self._call("partner_file", "file_attach", {
			"lead": lead, "filename": filename, "file_type": file_type,
			"content_base64": base64.b64encode(content).decode(),
		})

	def attach_bulk(self, files):
		return self._call("partner_file", "file_attach_bulk", {"files": files})

	def get(self, name):
		return self._call("partner_file", "file_get", params={"name": name})

	def get_bulk(self, names):
		return self._call("partner_file", "file_get_bulk", {"names": names})

	def list(self, lead):
		return self._call("partner_file", "file_list", params={"lead": lead, "limit": 50})

	def delete(self, name):
		return self._call("partner_file", "file_delete", {"name": name})

	def delete_bulk(self, names):
		return self._call("partner_file", "file_delete_bulk", {"names": names})


def _percentiles(values):
	if not values:
		return {}
	ordered = sorted(values)

	def at(p):
		return round(ordered[min(int(len(ordered) * p / 100), len(ordered) - 1)], 1)

	return {"n": len(ordered), "p50": at(50), "p90": at(90), "p95": at(95), "p99": at(99),
	        "max": round(ordered[-1], 1)}


def run(account, want_leads, batch, clamav):
	token, grain = partner_token(account)
	api = Partner(token)
	suite = Suite(api)
	creds = lsq_creds(account)

	call, body = api.get("partner_file", "file_schema", {})
	if not call.ok:
		raise SystemExit(f"file_schema failed: {call.code} {call.message}")
	print(f"[{account}] grain={grain}  clamav={'ON' if clamav else 'OFF'}")
	print(f"  file_schema: {len(body['data'].get('fields') or [])} fields, "
	      f"max {body['data'].get('bytes', '')[-30:].strip()}")

	# The leads this API just created, addressed the way a partner addresses them.
	call, body = api.get("partner", "lead_list", {"limit": want_leads})
	if not call.ok:
		raise SystemExit(f"lead_list failed: {call.code} {call.message}")
	leads = body["data"]["leads"]
	by_external = {row.get("external_id"): row["name"] for row in leads if row.get("external_id")}
	print(f"  {len(leads)} lead(s) on the grain, {len(by_external)} carrying their LSQ id")

	# 1. HARVEST. Minted now, downloaded now.
	docs, gone = [], Counter()
	started = time.time()
	for i, (prospect, lead) in enumerate(list(by_external.items())[:want_leads], 1):
		for row in _fresh_urls(creds, prospect):
			content, err = _download(row["url"])
			if err:
				gone[err] += 1
				continue
			docs.append({"lead": lead, "filename": row["filename"], "content": content,
			             "bytes": len(content)})
		if i % 5 == 0:
			print(f"  harvest: {i}/{min(len(by_external), want_leads)} leads, "
			      f"{len(docs)} document(s), {sum(gone.values())} unreachable")
	print(f"  harvested {len(docs)} real document(s) in {time.time() - started:.0f}s"
	      f"  (unreachable at LSQ: {dict(gone) or 'none'})")

	if not docs:
		print("  NO DOCUMENTS — the file lane cannot be proven on this slice.")
		return None

	sizes = sorted(d["bytes"] for d in docs)
	print(f"  sizes: median {sizes[len(sizes) // 2] / 1024:.0f} KB, "
	      f"largest {sizes[-1] / 1024:.0f} KB")

	lead_one = docs[0]["lead"]

	# 2. SINGLE ATTACH.
	print()
	print("  --- single ---")
	call, body = suite.attach(lead_one, docs[0]["filename"], docs[0]["content"], "lsq-document")
	if not call.ok:
		print(f"  file_attach FAILED: {call.code} {call.message}")
		return None
	first = body["data"]["name"]
	print(f"  file_attach        -> {first}  is_private={body['data'].get('is_private')}")

	suite.get(first)
	suite.list(lead_one)

	# 3. THE VIRUS. The only proof the scanner is in the path.
	call, body = suite.attach(lead_one, "eicar.txt", EICAR.encode(), "virus-probe")
	verdict = "REJECTED" if not call.ok else "ACCEPTED"
	print(f"  EICAR virus        -> {verdict}"
	      f"{'  (' + str(call.code) + ')' if not call.ok else '  <-- SCANNER DID NOT CATCH IT'}")
	eicar_blocked = not call.ok
	if call.ok:  # it got in: clean it up so the grain is not left holding a virus
		suite.delete(body["data"]["name"])

	# 4. BULK ATTACH — the endpoint the load never reached.
	print()
	print("  --- bulk ---")
	made = []
	for chunk in (docs[i:i + batch] for i in range(0, len(docs), batch)):
		payload = [{"lead": d["lead"], "filename": d["filename"], "file_type": "lsq-document",
		            "content_base64": base64.b64encode(d["content"]).decode()} for d in chunk]
		call, body = suite.attach_bulk(payload)
		if not call.ok:
			print(f"  file_attach_bulk x{len(chunk)} FAILED: {call.code} {call.message}")
			continue
		summary = body.get("summary") or {}
		made += [r["data"]["name"] for r in body.get("results", []) if r.get("status") == "success"]
		print(f"  file_attach_bulk x{len(chunk):<3} -> {summary.get('succeeded')} ok, "
		      f"{summary.get('failed')} failed  ({suite.timings['file_attach_bulk'][-1]:.0f} ms)")

	if made:
		suite.get_bulk(made[:25])
		suite.list(lead_one)
		suite.delete_bulk(made[:5])
		for name in made[5:8]:
			suite.delete(name)

	# 5. REPORT.
	print()
	print(f"  {'endpoint':<24} {'n':>4} {'p50':>8} {'p90':>8} {'p95':>8} {'p99':>8} {'max':>9}")
	stats = {}
	for endpoint in sorted(suite.timings):
		p = _percentiles(suite.timings[endpoint])
		stats[endpoint] = p
		print(f"  {endpoint:<24} {p['n']:>4} {p['p50']:>8} {p['p90']:>8} {p['p95']:>8} "
		      f"{p['p99']:>8} {p['max']:>9}")

	print()
	print(f"  documents attached : {len(made) + 1}")
	print(f"  EICAR rejected     : {eicar_blocked}  (clamav={'ON' if clamav else 'OFF'})")
	print(f"  failures           : {len(suite.failures)}")
	for f in suite.failures[:5]:
		print(f"    {f['endpoint']}: {f['code']} {f['message'][:70]}")

	REPORTS.mkdir(parents=True, exist_ok=True)
	path = REPORTS / f"{account}.files.{'clamav' if clamav else 'noclamav'}.json"
	path.write_text(json.dumps({
		"account": account, "clamav": clamav, "documents": len(docs),
		"attached": len(made) + 1, "eicar_rejected": eicar_blocked,
		"median_kb": round(sizes[len(sizes) // 2] / 1024, 1),
		"unreachable_at_lsq": dict(gone),
		"latency_ms": stats,
		"failures": suite.failures[:50], "failure_total": len(suite.failures),
	}, indent=2) + "\n")
	print(f"  written: {path}")
	return stats


def main():
	ap = argparse.ArgumentParser(description="drive every file endpoint with real LSQ documents")
	ap.add_argument("accounts", nargs="*", default=["anaya"])
	ap.add_argument("--leads", type=int, default=20)
	ap.add_argument("--batch", type=int, default=10)
	ap.add_argument("--no-clamav", action="store_true",
	                help="label the run as scanner-off (the toggle itself is set by preflight)")
	args = ap.parse_args()
	for account in args.accounts:
		run(account, args.leads, args.batch, not args.no_clamav)


if __name__ == "__main__":
	main()
