# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Pull a bounded slice of live LSQ data onto this machine, once, for both tests to replay.

The pull is activity-first. Asking for leads first and then their activities selects the wrong slice:
most leads carry no activity on a mapped event, so a lead-first slice loads almost nothing and tests
almost nothing. Every mapped event code is swept, the leads those activities belong to are ranked by
how much they carry, and the richest are taken. Each chosen lead therefore arrives with the whole of
its activity history, which is what makes the load worth running.

LSQ is paced and read once. Notes and LSQ's own tasks are deliberately not fetched: the partner API
has no endpoint for either, so those reads would cost thousands of requests against someone else's
production system and test nothing.

    python -m tatva_connect.tests.loadtest.pull anaya tatvapractice --leads 1000
"""
import argparse
import json
import time
from collections import defaultdict

from tatva_connect.tests.loadtest.config import ACCOUNTS, account_dir, field_map, lsq_creds
from tatva_connect.tests.loadtest.lsq import LSQ, LSQReadOnlyError

PAGE = 1000
FROM_DATE = "2024-01-01 00:00:00"
TO_DATE = "2026-12-31 23:59:59"


def _event_codes(fmap):
	codes = list(fmap["activity_task_types"])
	codes += [c for c in (fmap.get("call_log_events") or {}) if c not in codes]
	return codes


def _sweep(account, client, codes, per_code_cap):
	"""Every activity on every mapped code, indexed by the lead it belongs to. Cached: the sweep is
	the expensive half of the pull and neither test should make LSQ serve it twice."""
	cache = account_dir(account) / "activities.jsonl"
	by_lead = defaultdict(list)

	if cache.exists():
		with cache.open() as fh:
			for line in fh:
				row = json.loads(line)
				by_lead[row["RelatedProspectId"]].append(row)
		print(f"[{account}] activity sweep restored from cache: "
		      f"{sum(len(v) for v in by_lead.values())} activities, {len(by_lead)} leads")
		return by_lead

	with cache.open("w") as fh:
		for code in codes:
			pulled, page = 0, 1
			while True:
				try:
					rows, total = client.activities_by_event(code, FROM_DATE, TO_DATE, page, PAGE)
				except LSQReadOnlyError as exc:
					print(f"  ! event {code} page {page}: {exc}")
					break
				if not rows:
					break
				for row in rows:
					pid = row.get("RelatedProspectId")
					if not pid:
						continue
					by_lead[pid].append(row)
					fh.write(json.dumps(row) + "\n")
				pulled += len(rows)
				if len(rows) < PAGE or pulled >= total:
					break
				if per_code_cap and pulled >= per_code_cap:
					print(f"  event {code}: stopped at the {per_code_cap} cap ({total} exist)")
					break
				page += 1
			if pulled:
				print(f"  event {code:<5} {pulled:>7} activities   ({len(by_lead)} leads so far, "
				      f"{client.calls} LSQ reads)")

	print(f"[{account}] sweep done: {sum(len(v) for v in by_lead.values())} activities "
	      f"across {len(by_lead)} leads")
	return by_lead


def pull(account, want_leads, per_code_cap):
	fmap = field_map(account)
	client = LSQ(lsq_creds(account))
	codes = _event_codes(fmap)
	started = time.time()
	print(f"[{account}] sweeping {len(codes)} mapped event code(s); want {want_leads} leads")

	by_lead = _sweep(account, client, codes, per_code_cap)

	ranked = sorted(by_lead, key=lambda p: len(by_lead[p]), reverse=True)[:want_leads]
	print(f"[{account}] taking the {len(ranked)} richest leads "
	      f"({sum(len(by_lead[p]) for p in ranked)} activities between them)")

	out = account_dir(account) / "bundles.jsonl"
	kept, missing = 0, 0
	with out.open("w") as fh:
		for i, pid in enumerate(ranked, 1):
			try:
				lead = client.lead(pid)
			except LSQReadOnlyError as exc:
				print(f"  ! lead {pid}: {exc}")
				missing += 1
				continue
			if not lead:
				missing += 1
				continue
			fh.write(json.dumps({"prospect_id": pid, "lead": lead, "activities": by_lead[pid]}) + "\n")
			kept += 1
			if i % 100 == 0:
				print(f"  {i}/{len(ranked)} leads bundled ({client.calls} LSQ reads, "
				      f"{time.time() - started:.0f}s)")

	acts = sum(len(by_lead[p]) for p in ranked)
	print(f"[{account}] {kept} bundles -> {out}  ({missing} leads not retrievable)")
	print(f"[{account}] {acts} activities, {client.calls} LSQ reads, {time.time() - started:.0f}s")


def main():
	ap = argparse.ArgumentParser(description="pull live LSQ data for the partner API load test")
	ap.add_argument("accounts", nargs="*", default=list(ACCOUNTS))
	ap.add_argument("--leads", type=int, default=1000)
	ap.add_argument("--per-code", type=int, default=0, help="cap activities per event code (0 = all)")
	args = ap.parse_args()
	for account in (args.accounts or ACCOUNTS):
		pull(account, args.leads, args.per_code)


if __name__ == "__main__":
	main()
