# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""One command, from a laptop, against a real deployment: pull live LSQ, then load it over the API.

The local harness keeps the pull and the load apart because two tests replay one cached slice. Here the
question is a different one, and it is the question UAT exists to answer: can a partner sitting outside
the network, holding nothing but a token, take a real grain out of LeadSquared and put it into the CRM.
So the two halves are one command against one target, and nothing about the site is assumed. There is no
bench here, no database handle, and no way to see what landed except to ask the API for it back — which
is exactly the position a real partner is in, and the reason this run is worth more than the local one.

A grain is a process. Run two side by side and the concurrency is real: two partners, two tokens, two
rate budgets, one server.

    python -m tatva_connect.tests.loadtest.uat --grain anaya         --leads 50 --base https://uat.example.in --site uat.example.in
    python -m tatva_connect.tests.loadtest.uat --grain tatvapractice --leads 50 --base https://uat.example.in --site uat.example.in

The slice is activity-first: every mapped event code is swept, leads are ranked by how much history they
carry, and the richest are taken. Fifty random leads would be fifty leads and almost nothing else, and
would leave most of the API untouched.

This run creates and does not clean up. Every lead it makes is listed in its report, because on a shared
deployment the only way to find what you left behind is to have written it down.
"""
import argparse
import sys

from tatva_connect.tests.loadtest import pull as pull_mod
from tatva_connect.tests.loadtest import run as run_mod
from tatva_connect.tests.loadtest.config import ACCOUNTS, BASE_URL, SITE_HOST, account_dir

LOCAL = ("localhost", "127.0.0.1", "dev.localhost")


def confirm(base, host, account, leads, assume_yes):
	"""A deployment is not a bench. The target is stated and consented to before a single row is written."""
	local = any(token in base for token in LOCAL) and any(token in host for token in LOCAL)
	print(f"target : {base}   (Host: {host})")
	print(f"grain  : {account}")
	print(f"leads  : {leads}  (richest by activity count)")
	if local or assume_yes:
		return
	print()
	print("This is not a local bench. The run will CREATE real leads, activities, calls and files")
	print("there, owned by the partner user, and it will not remove them afterwards.")
	answer = input("Type the site host to proceed: ").strip()
	if answer != host:
		raise SystemExit("aborted -- host not confirmed")


def main():
	ap = argparse.ArgumentParser(
		description="pull live LSQ and load it through the partner API of a real deployment")
	ap.add_argument("--grain", required=True, choices=list(ACCOUNTS),
	                help="one grain per process; run two processes to load two grains at once")
	ap.add_argument("--leads", type=int, default=50)
	ap.add_argument("--base", default=BASE_URL, help="e.g. https://uat.example.in")
	ap.add_argument("--site", default=SITE_HOST, help="the Host header the site answers to")
	ap.add_argument("--tokens", default="partner-api-tokens.uat.json",
	                help="token file under docs/go-live/7-migrate-data/.creds/")
	ap.add_argument("--env", default="uat", help="tags the report file")

	ap.add_argument("--per-code", type=int, default=5000,
	                help="cap activities pulled per LSQ event code (0 = every one)")
	ap.add_argument("--fresh", action="store_true",
	                help="re-sweep LSQ instead of reusing the cached activity sweep")
	ap.add_argument("--skip-pull", action="store_true", help="load the slice already on disk")

	ap.add_argument("--rate", type=int, default=300,
	                help="calls per minute. Deliberately gentle: this is the first traffic UAT has seen")
	ap.add_argument("--workers", type=int, default=4)
	ap.add_argument("--bulk", action="store_true", help="send through the bulk endpoints, as a backfill would")
	ap.add_argument("--batch", type=int, default=100)
	ap.add_argument("--file-batch", type=int, default=10)
	ap.add_argument("--no-files", action="store_true",
	                help="skip the attachment phase (each file is a download plus a virus scan)")
	ap.add_argument("--replay", action="store_true",
	                help="second pass, same idempotency keys: it must create nothing")
	ap.add_argument("--yes", action="store_true", help="skip the confirmation")
	args = ap.parse_args()

	account = args.grain
	confirm(args.base, args.site, account, args.leads, args.yes)

	if not args.skip_pull:
		if args.fresh:
			cache = account_dir(account) / "activities.jsonl"
			cache.unlink(missing_ok=True)
			print(f"[{account}] cached sweep dropped; re-reading LSQ")
		pull_mod.pull(account, args.leads, args.per_code)
		print()

	loader = run_mod.load_bulk if args.bulk else run_mod.load_account
	common = dict(base=args.base, host=args.site, tokens=args.tokens, env=args.env,
	              files=not args.no_files)
	if args.bulk:
		result = loader(account, args.leads, args.replay, args.rate, args.batch,
		                args.file_batch, args.workers, **common)
	else:
		result = loader(account, args.leads, args.replay, args.workers, args.rate, **common)

	run_mod.report(result)
	return 1 if result["failures"] else 0


if __name__ == "__main__":
	sys.exit(main())
