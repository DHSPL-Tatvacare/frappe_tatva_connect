# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Drive the pulled LSQ slice into the CRM through the partner API, and time every call.

Per lead the order is the order a partner would use, because each step needs the address the last one
handed back: the lead is created, its activities are created against it, its calls are logged against
it, and its files are attached to the activities they came from.

Every write carries an Idempotency-Key derived from the source record's own id, so the run is
replayable: a second pass must create nothing and replay the stored responses instead. That is the
test, not a convenience.

    python -m tatva_connect.tests.live.load.run anaya --leads 250
    python -m tatva_connect.tests.live.load.run anaya --leads 250 --replay
"""
import argparse
import base64
import json
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

from tatva_connect.tests.live.load import shape
from tatva_connect.tests.live.load.client import Partner, idempotency_key
from tatva_connect.tests.live.load.config import (
	ACCOUNTS,
	REPORTS,
	account_dir,
	field_map,
	lsq_creds,
	partner_token,
)
from tatva_connect.tests.live.load.files import _download, _fresh_urls
from tatva_connect.tests.live.load.log import Logger


def _lead_files(creds, prospect_id, lead_name, logger=None):
	"""Mint LSQ's presigned document URLs for ONE lead now, download the bytes, and shape file_attach
	bodies that carry the content inline (content_base64).

	Files must be minted here, not read from the pulled bundle: the bulk activity sweep the pull uses
	returns no attachment URLs, and LSQ presigns for ~30 minutes, so a URL harvested earlier is dead.
	Content is sent inline rather than as a file_url so the run does not depend on the target server
	being able to reach LSQ's storage. A gone/expired object is recorded and skipped -- that is a fact
	about LSQ, not a failure of the API under test. Files home on the lead (the partner file endpoint's
	default), matching the standalone file suite.
	"""
	out, gone = [], 0
	for row in _fresh_urls(creds, prospect_id):
		content, err = _download(row["url"])
		if err or content is None:
			gone += 1
			if logger:
				logger.event("file", "download", 0, 0.0, False, f"{err} {row['filename']}")
			continue
		out.append({
			"lead": lead_name,
			"filename": row["filename"],
			"file_type": "lsq-document",
			"content_base64": base64.b64encode(content).decode(),
			"external_id": row["path"],
			"_source_id": row["path"],
		})
	return out, gone


class Pacer:
	"""Keeps the whole run under the partner's call rate, so a 429 means a real defect rather than
	this harness having simply driven faster than the contract allows. Leads run in parallel because a
	real partner would; the pacer is what keeps that from turning into the stress test."""

	def __init__(self, per_minute):
		self.interval = 60.0 / max(per_minute, 1)
		self.lock = threading.Lock()
		self.next_slot = time.monotonic()

	def take(self):
		with self.lock:
			now = time.monotonic()
			slot = max(self.next_slot, now)
			self.next_slot = slot + self.interval
		delay = slot - time.monotonic()
		if delay > 0:
			time.sleep(delay)


def _payload(body):
	return {k: v for k, v in body.items() if not k.startswith("_")}


def _chunks(items, size):
	for i in range(0, len(items), size):
		yield items[i:i + size]


def _batch_key(account, entity, batch):
	"""One idempotency key per batch, derived from the records in it, so a re-sent batch replays."""
	return idempotency_key(account, entity, "|".join(str(b.get("_source_id", "")) for b in batch))


def load_bulk(account, limit, replay, per_minute, batch_size, file_batch, workers,
              base=None, host=None, tokens=None, env=None, files=True, logger=None):
	"""The backfill path: the same records, sent through the bulk endpoints.

	A partner moving a hundred thousand records does not send a hundred thousand requests. The bulk
	endpoints exist for this, they charge one call against the rate limit and N rows against the
	volume quota, and each record is enforced in its own savepoint so one bad row does not sink the
	batch. That partial-success contract is exactly what needs proving at scale.
	"""
	log = logger or Logger()
	fmap = field_map(account)
	token, grain = partner_token(account, tokens)
	api = Partner(token, base=base, host=host, logger=log)
	creds = lsq_creds(account) if files else None
	# Bulk has its OWN server bucket: 1 call per 5s, burst 1. More workers or a faster pace only
	# manufacture 429s that retry to the same throughput. Pin ONE caller paced to that bucket, so the
	# run crawls cleanly instead of drowning in rejections. Raise --bulk-rate only if the operator
	# raised bulk_rate/bulk_burst in CRM Partner API Settings for the window.
	workers = 1
	pacer = Pacer(per_minute)

	bundles = [json.loads(line) for line in (account_dir(account) / "bundles.jsonl").open()]
	if limit:
		bundles = bundles[:limit]

	# Discovery before ingestion — for EVERY entity, not just the lead. Reading lead_schema and
	# assuming the rest is how 3,844 calls were sent with LSQ's word for a direction (Incoming) when
	# call_schema plainly publishes the two the API takes (Inbound, Outbound).
	#
	# activity_schema is not here: it is discovery BY LEAD (the types available depend on the lead's
	# grain and program), so it needs a lead that exists. It is fetched after the lead phase.
	schemas = {}
	for entity, module in (("lead", "partner"), ("call", "partner_call"), ("file", "partner_file")):
		call, body = api.get(module, f"{entity}_schema", {})
		if not call.ok:
			raise SystemExit(f"[{account}] {entity}_schema failed: {call.code} {call.message}")
		schemas[entity] = body["data"]

	children_spec = schemas["lead"].get("children") or {}
	shape.assert_allowed(schemas["call"], "direction", set(shape.CALL_DIRECTION.values()), "call")
	shape.assert_allowed(schemas["call"], "status", set(shape.CALL_STATUS.values()), "call")

	acts = sum(len(b.get("activities") or []) for b in bundles)
	log.line(f"[{account}] grain={grain}  {len(bundles)} lead(s), {acts} source activities  "
	         f"BULK x{batch_size} paced at {per_minute}/min  {'REPLAY' if replay else 'LOAD'}", "bold")
	log.line(f"[{account}] target {api.base}  (Host: {api.host})", "bright_black")

	actions = Counter()
	dropped = defaultdict(int)
	failures = []
	lock = threading.Lock()
	started = time.time()

	def send(module, fn, key, batch, entity, phase):
		"""One bulk call. Returns (batch, per-record results) so the caller can map results back to
		the records that produced them -- the results are index-aligned with the batch it sent."""
		pacer.take()
		payload = {key: [_payload(b) for b in batch]}
		call, body = api.post(module, fn, payload, idem=_batch_key(account, entity, batch))
		if not call.ok:
			with lock:
				failures.append({"phase": phase, "code": call.code, "message": call.message,
				                 "records": len(batch)})
			return batch, []
		results = body.get("results") or []
		with lock:
			for r in results:
				if r.get("status") == "success":
					actions[f"{phase}:{r.get('action')}"] += 1
				else:
					err = r.get("error") or {}
					failures.append({"phase": phase, "index": r.get("index"),
					                 "code": err.get("code") or "record_failed",
					                 "message": (err.get("message") or "")[:200]})
		return batch, results

	def run_phase(module, fn, key, items, entity, phase, size):
		"""Every batch in a phase concurrently. Batches within a phase are independent -- only the
		phases depend on each other, because each needs the addresses the last one handed back.
		Sending them one at a time leaves the server idle between calls and makes the run's throughput
		a measure of this harness rather than of the API."""
		batches = list(_chunks(items, size))
		if not batches:
			return {}
		log.phase(f"{phase}  ({len(items)} records, x{size} bulk)")
		names, done = {}, [0]
		with ThreadPoolExecutor(max_workers=workers) as pool:
			for batch, results in pool.map(
					lambda b: send(module, fn, key, b, entity, phase), batches):
				for r in results:
					if r.get("status") == "success":
						names[batch[r["index"]]["_source_id"]] = r["data"]["name"]
				done[0] += len(batch)
				if done[0] % (size * 20) < size:
					secs = time.time() - started
					log.progress(phase, done[0], len(items),
					             f"{done[0] / max(secs, 0.001):.0f} rec/s  {len(failures)} failed")
		log.progress(phase, len(names), len(items), f"created  ({time.time() - started:.0f}s)")
		return names

	# 1. LEADS. The addresses everything else hangs from.
	lead_items = []
	for bundle in bundles:
		body = shape.lead_body(bundle, fmap, children_spec, dropped)
		body["_source_id"] = bundle["prospect_id"]
		lead_items.append(body)
	lead_name = run_phase("partner", "lead_create_bulk", "leads", lead_items, "lead", "lead", batch_size)

	# 2. ACTIVITIES. Their schema is discovery BY LEAD, so it is asked for now that a lead exists —
	#    and it names the task types this grain actually accepts, which is the vocabulary the shaper
	#    is about to send. Checking it here is the same rule that would have caught the call bug.
	if lead_name:
		probe = next(iter(lead_name.values()))
		call, body = api.get("partner_activity", "activity_schema", {"lead": probe})
		if not call.ok:
			raise SystemExit(f"[{account}] activity_schema failed: {call.code} {call.message}")
		schemas["activity"] = body["data"]
		offered = {t["name"] for t in schemas["activity"].get("task_types") or []}
		wanted = set(fmap["activity_task_types"].values())
		log.line(f"  activity types: {len(offered)} offered by the API for this grain, "
		         f"{len(wanted)} in the field map", "bright_black")

	activity_items = []
	for bundle in bundles:
		name = lead_name.get(bundle["prospect_id"])
		if name:
			activity_items += shape.activity_bodies(bundle, fmap, name)
	run_phase("partner_activity", "activity_create_bulk", "activities",
	          activity_items, "activity", "activity", batch_size)

	# 3. CALLS.
	call_items = []
	for bundle in bundles:
		name = lead_name.get(bundle["prospect_id"])
		if name:
			call_items += shape.call_bodies(bundle, fmap, name)
	run_phase("partner_call", "call_create_bulk", "calls", call_items, "call", "call", batch_size)

	# 4. FILES. Minted live per lead (the bundle carries no URLs; LSQ presigns for ~30 min), downloaded
	#    here, and sent inline. The batch is deliberately smaller: each file is a download plus a virus
	#    scan, so a hundred in one request is a request that runs for minutes.
	file_items, files_gone = [], 0
	if files:
		log.phase(f"files: minting LSQ urls for {len(lead_name)} lead(s)")
		for bundle in bundles:
			name = lead_name.get(bundle["prospect_id"])
			if not name:
				continue
			docs, gone = _lead_files(creds, bundle["prospect_id"], name, log)
			file_items += docs
			files_gone += gone
		log.line(f"  {len(file_items)} document(s) harvested, {files_gone} unreachable at LSQ",
		         "bright_black")
		run_phase("partner_file", "file_attach_bulk", "files", file_items, "file", "file", file_batch)

	return {
		"account": account, "grain": grain, "replay": replay, "leads": len(bundles),
		"elapsed_s": round(time.time() - started, 1), "actions": dict(actions),
		"dropped_rows": dict(dropped), "failures": failures, "calls": api.calls,
		"env": env, "target": api.base, "created_leads": dict(lead_name),
		"files_unreachable": files_gone,
		"offered": {"lead": len(lead_items), "activity": len(activity_items),
		            "call": len(call_items), "file": len(file_items)},
	}


def load_account(account, limit, replay, workers, per_minute,
                 base=None, host=None, tokens=None, env=None, files=True, logger=None):
	log = logger or Logger()
	fmap = field_map(account)
	token, grain = partner_token(account, tokens)
	api = Partner(token, base=base, host=host, logger=log)
	creds = lsq_creds(account) if files else None
	pacer = Pacer(per_minute)

	bundles_path = account_dir(account) / "bundles.jsonl"
	if not bundles_path.exists():
		raise SystemExit(f"no pulled data at {bundles_path} -- run pull.py first")

	bundles = [json.loads(line) for line in bundles_path.open()]
	if limit:
		bundles = bundles[:limit]

	# Discovery before ingestion: the API declares which children are multi-row and what key addresses
	# their rows. The harness reads that rather than assuming it.
	call, schema = api.get("partner", "lead_schema", {})
	if not call.ok:
		raise SystemExit(f"[{account}] lead_schema failed: {call.code} {call.message}")
	children_spec = schema["data"].get("children") or {}

	acts = sum(len(b.get("activities") or []) for b in bundles)
	log.line(f"[{account}] grain={grain}  {len(bundles)} lead(s), {acts} source activities  "
	         f"{workers} workers paced at {per_minute}/min  {'REPLAY' if replay else 'LOAD'}", "bold")
	log.line(f"[{account}] target {api.base}  (Host: {api.host})", "bright_black")
	files_gone = [0]

	actions = Counter()
	dropped = defaultdict(int)
	failures = []
	created = {}
	lock = threading.Lock()
	done = [0]
	started = time.time()

	def record(phase, pid, source, call):
		with lock:
			if call.ok:
				actions[f"{phase}:{call.action}"] += 1
				return True
			failures.append({"phase": phase, "prospect": pid, "source": source,
			                 "code": call.code, "message": call.message})
			return False

	def one_lead(bundle):
		"""A lead and everything hanging off it, in the only order the API allows: each step needs the
		address the step before it handed back."""
		pid = bundle["prospect_id"]
		with lock:
			body_out = _payload(shape.lead_body(bundle, fmap, children_spec, dropped))

		pacer.take()
		call, body = api.post("partner", "lead_create", body_out,
		                      idem=idempotency_key(account, "lead", pid))
		if not record("lead", pid, pid, call):
			return
		lead_name = body["data"]["name"]
		with lock:
			created[str(pid)] = lead_name

		for item in shape.activity_bodies(bundle, fmap, lead_name):
			pacer.take()
			call, _body = api.post("partner_activity", "activity_create", _payload(item),
			                       idem=idempotency_key(account, "activity", item["_source_id"]))
			record("activity", pid, item["_source_id"], call)

		for item in shape.call_bodies(bundle, fmap, lead_name):
			pacer.take()
			call, _body = api.post("partner_call", "call_create", _payload(item),
			                       idem=idempotency_key(account, "call", item["_source_id"]))
			record("call", pid, item["_source_id"], call)

		if files:
			docs, gone = _lead_files(creds, pid, lead_name, log)
			with lock:
				files_gone[0] += gone
			for item in docs:
				pacer.take()
				call, _body = api.post("partner_file", "file_attach", _payload(item),
				                       idem=idempotency_key(account, "file", item["_source_id"]))
				record("file", pid, item["_source_id"], call)

		with lock:
			done[0] += 1
			if done[0] % 50 == 0:
				secs = time.time() - started
				log.progress("leads", done[0], len(bundles),
				             f"{len(api.calls)} calls  {len(api.calls) / max(secs, 0.001):.0f} req/s  "
				             f"{len(failures)} failed")

	with ThreadPoolExecutor(max_workers=workers) as pool:
		list(pool.map(one_lead, bundles))

	elapsed = time.time() - started
	return {
		"account": account,
		"grain": grain,
		"replay": replay,
		"leads": len(bundles),
		"elapsed_s": round(elapsed, 1),
		"actions": dict(actions),
		"dropped_rows": dict(dropped),
		"failures": failures,
		"calls": api.calls,
		"env": env,
		"target": api.base,
		"created_leads": created,
		"files_unreachable": files_gone[0],
	}


def _percentiles(values):
	if not values:
		return {}
	ordered = sorted(values)

	def at(p):
		idx = min(int(len(ordered) * p / 100), len(ordered) - 1)
		return round(ordered[idx], 1)

	return {"n": len(ordered), "p50": at(50), "p90": at(90), "p99": at(99), "max": round(ordered[-1], 1)}


def report(result, logger=None):
	log = logger or Logger()
	by_endpoint = defaultdict(list)
	for call in result["calls"]:
		by_endpoint[call.endpoint].append(call.ms)

	print()
	print(f"===== {result['account']} ({'replay' if result['replay'] else 'load'}) =====")
	print(f"  {result['leads']} leads, {len(result['calls'])} API calls, {result['elapsed_s']}s")
	if result.get("target"):
		print(f"  target: {result['target']}")
	print()
	print("  outcome:")
	for key in sorted(result["actions"]):
		print(f"    {key:<22} {result['actions'][key]}")
	if not result["actions"]:
		print("    (nothing succeeded)")

	if result["dropped_rows"]:
		print()
		print("  child rows dropped (no source for the key the API requires):")
		for cf, n in sorted(result["dropped_rows"].items()):
			print(f"    {cf:<34} {n}")

	print()
	print(f"  latency ms          {'n':>6} {'p50':>7} {'p90':>7} {'p99':>7} {'max':>8}")
	for endpoint in sorted(by_endpoint):
		p = _percentiles(by_endpoint[endpoint])
		print(f"    {endpoint:<32} {p['n']:>4} {p['p50']:>7} {p['p90']:>7} {p['p99']:>7} {p['max']:>8}")

	if result["failures"]:
		codes = Counter(f["code"] for f in result["failures"])
		print()
		print(f"  failures: {len(result['failures'])}")
		for code, n in codes.most_common():
			sample = next(f for f in result["failures"] if f["code"] == code)
			print(f"    {code:<22} {n:>5}   e.g. {sample['phase']}: {sample['message'][:80]}")
	else:
		print()
		print("  failures: 0")

	REPORTS.mkdir(parents=True, exist_ok=True)
	suffix = "replay" if result["replay"] else "load"
	env = result.get("env")
	stem = f"{result['account']}.{env}.{suffix}" if env else f"{result['account']}.{suffix}"
	path = REPORTS / f"{stem}.json"
	path.write_text(json.dumps({
		"account": result["account"],
		"grain": result["grain"],
		"target": result.get("target"),
		"leads": result["leads"],
		"elapsed_s": result["elapsed_s"],
		"actions": result["actions"],
		"dropped_rows": result["dropped_rows"],
		"latency_ms": {e: _percentiles(v) for e, v in by_endpoint.items()},
		"failures": result["failures"][:200],
		"failure_total": len(result["failures"]),
		"files_unreachable": result.get("files_unreachable", 0),
		# The run creates and does not clean up. On a shared deployment the only way to find what it
		# left behind is to have written it down, so it is written down.
		"created_leads": result.get("created_leads") or {},
	}, indent=2) + "\n")
	print(f"  written: {path}")

	# The at-a-glance: what actually landed, per entity, plus the file-lane's unreachable count.
	created = defaultdict(int)
	for key, n in result["actions"].items():
		created[key.split(":", 1)[0]] += n
	rows = [(f"{entity} created", str(created.get(entity, 0))) for entity in ("lead", "activity", "call", "file")]
	rows.append(("files unreachable at LSQ", str(result.get("files_unreachable", 0))))
	rows.append(("failures", str(len(result["failures"]))))
	rows.append(("API calls", str(len(result["calls"]))))
	rows.append(("elapsed", f"{result['elapsed_s']}s"))
	log.summary(f"{result['account']} — {'replay' if result['replay'] else 'load'}", rows,
	            note=f"target {result.get('target', '')}")


def main():
	ap = argparse.ArgumentParser(description="load the pulled LSQ slice through the partner API")
	ap.add_argument("accounts", nargs="*", default=list(ACCOUNTS))
	ap.add_argument("--leads", type=int, default=0, help="cap leads (0 = all pulled)")
	ap.add_argument("--workers", type=int, default=4, help="singular mode only")
	ap.add_argument("--rate", type=int, default=100,
	                help="calls per minute. Server caps a token at 120/min (240 burst), 300/min global")
	ap.add_argument("--bulk", action="store_true",
	                help="send through the bulk endpoints, as a real backfill would")
	ap.add_argument("--batch", type=int, default=25, help="records per bulk call (server ceiling is 25)")
	ap.add_argument("--file-batch", type=int, default=5,
	                help="files per bulk call (server ceiling is 5): each is a download plus a virus scan")
	ap.add_argument("--replay", action="store_true",
	                help="second pass with the same keys: must create nothing")
	ap.add_argument("--no-files", action="store_true", help="skip the file phase")
	ap.add_argument("--verbose", action="store_true", help="stream every call as it happens")
	args = ap.parse_args()

	logger = Logger(verbose=args.verbose)
	for account in (args.accounts or ACCOUNTS):
		if args.bulk:
			result = load_bulk(account, args.leads, args.replay, args.rate, args.batch,
			                   args.file_batch, args.workers, files=not args.no_files, logger=logger)
		else:
			result = load_account(account, args.leads, args.replay, args.workers, args.rate,
			                      files=not args.no_files, logger=logger)
		report(result, logger)


if __name__ == "__main__":
	main()
