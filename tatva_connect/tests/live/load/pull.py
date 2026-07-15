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

    python -m tatva_connect.tests.live.load.pull anaya tatvapractice --leads 1000
"""
import argparse
import json
import time
from collections import defaultdict

from tatva_connect.tests.live.load.config import ACCOUNTS, account_dir, field_map, lsq_creds
from tatva_connect.tests.live.load.log import Logger
from tatva_connect.tests.live.load.lsq import LSQ, LSQReadOnlyError

PAGE = 1000
FROM_DATE = "2024-01-01 00:00:00"
TO_DATE = "2026-12-31 23:59:59"


def _seen_path(account):
	"""Ledger of prospect ids already taken into a batch, so --skip-pulled can page past them."""
	return account_dir(account) / "seen.json"


def _load_seen(account):
	path = _seen_path(account)
	if not path.exists():
		return set()
	try:
		return set(json.loads(path.read_text()))
	except (ValueError, OSError):
		return set()


def _add_seen(account, ids):
	seen = _load_seen(account)
	seen.update(ids)
	_seen_path(account).write_text(json.dumps(sorted(seen)) + "\n")


def reset_seen(account):
	_seen_path(account).unlink(missing_ok=True)


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


def pull(account, want_leads, per_code_cap, logger=None, skip_pulled=False):
	log = logger or Logger()
	fmap = field_map(account)
	client = LSQ(lsq_creds(account), logger=log)
	codes = _event_codes(fmap)
	started = time.time()
	log.phase(f"pull {account}: sweeping {len(codes)} mapped event code(s); want {want_leads} leads")

	by_lead = _sweep(account, client, codes, per_code_cap)

	# Richest-first is deterministic, so re-running takes the SAME leads. --skip-pulled pages past the
	# ids already taken (the ledger) to the next fresh batch, so each run loads new records on Frappe.
	seen = _load_seen(account) if skip_pulled else set()
	ordered = sorted(by_lead, key=lambda p: len(by_lead[p]), reverse=True)
	ranked = [p for p in ordered if p not in seen][:want_leads]
	if skip_pulled and not ranked:
		log.line(f"[{account}] no fresh leads left — all {len(seen)} pulled. Reset with --reset-seen.", "red")
		return 0
	if skip_pulled:
		log.line(f"[{account}] skip-pulled: {len(seen)} already taken; taking the next {len(ranked)} "
		         f"({sum(len(by_lead[p]) for p in ranked)} activities between them)", "bright_black")
		_add_seen(account, ranked)  # mark attempted (incl. any not retrievable) so the next run advances
	else:
		log.line(f"[{account}] taking the {len(ranked)} richest leads "
		         f"({sum(len(by_lead[p]) for p in ranked)} activities between them)", "bright_black")

	out = account_dir(account) / "bundles.jsonl"
	kept, missing = 0, 0
	with out.open("w") as fh:
		for i, pid in enumerate(ranked, 1):
			try:
				lead = client.lead(pid)
			except LSQReadOnlyError as exc:
				log.line(f"  ! lead {pid}: {exc}", "red")
				missing += 1
				continue
			if not lead:
				missing += 1
				continue
			fh.write(json.dumps({"prospect_id": pid, "lead": lead, "activities": by_lead[pid]}) + "\n")
			kept += 1
			if i % 100 == 0:
				log.progress("bundled", i, len(ranked),
				             f"{client.calls} LSQ reads, {time.time() - started:.0f}s")

	acts = sum(len(by_lead[p]) for p in ranked)
	log.line(f"[{account}] {kept} bundles -> {out}  ({missing} leads not retrievable)", "bright_black")
	log.line(f"[{account}] {acts} activities, {client.calls} LSQ reads, {time.time() - started:.0f}s",
	         "bright_black")
	return kept


def main():
	ap = argparse.ArgumentParser(description="pull live LSQ data for the partner API load test")
	ap.add_argument("accounts", nargs="*", default=list(ACCOUNTS))
	ap.add_argument("--leads", type=int, default=1000)
	ap.add_argument("--per-code", type=int, default=0, help="cap activities per event code (0 = all)")
	ap.add_argument("--skip-pulled", action="store_true",
	                help="page past prospect ids already taken into a batch (ledger: data/<grain>/seen.json)")
	ap.add_argument("--reset-seen", action="store_true", help="clear the seen ledger, then pull from the top")
	ap.add_argument("--verbose", action="store_true", help="stream every LSQ call as it happens")
	args = ap.parse_args()
	logger = Logger(verbose=args.verbose)
	for account in (args.accounts or ACCOUNTS):
		if args.reset_seen:
			reset_seen(account)
		pull(account, args.leads, args.per_code, logger=logger, skip_pulled=args.skip_pulled)


if __name__ == "__main__":
	main()
