# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The UAT live-VAPT entrypoint — three layers, one command each.

  LAYER 1 (bench, oracle)   what SHOULD happen, per persona x endpoint, exported as expectations.json
  LAYER 2 (laptop, HTTP)    what DOES happen on the live URL, recorded as a corpus JSONL
  LAYER 3 (anywhere)        diff -> CORRECT / ESCALATION / OVER_BLOCK

Nothing here re-implements the attack surface: the cases come from `tests/authz/registry` (the tuples
built out of the VAPT), the personas from `tests/authz/roster`, the transport from `tests/authz/
http_engine`, and the "did it do the thing" judgment from `tests/authz/test_endpoint_sweep`.

    # 1. on the bench (once per code change)
    bench --site dev.localhost execute tatva_connect.tests.vapt_live.baseline.build

    # 2+3. from anywhere, against the live target in .creds/uat.json
    python -m tatva_connect.tests.vapt_live.run
"""
import argparse

from . import attack, compare, config


def main(argv=None):
	ap = argparse.ArgumentParser(description="Live VAPT replay against a non-prod target")
	ap.add_argument("--phase", choices=["attack", "compare", "all"], default="all")
	ap.add_argument("--corpus", help="existing corpus JSONL (for --phase compare)")
	ap.add_argument("--allow-unsafe-host", action="store_true",
	                help="override the non-prod host guard (never for production)")
	args = ap.parse_args(argv)

	corpus = args.corpus
	if args.phase in ("attack", "all"):
		cfg = config.load(allow_unsafe_host=args.allow_unsafe_host)
		print(f"[run] target={cfg['base']} host={cfg['host']} personas={sorted(cfg['personas'])}")
		corpus = attack.run(cfg)
	if args.phase in ("compare", "all"):
		if not corpus:
			raise SystemExit("--phase compare needs --corpus <path>")
		report = compare.run(corpus)
		if report["escalations"]:
			raise SystemExit(1)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
