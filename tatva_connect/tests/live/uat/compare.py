# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""LAYER 3 — diff the LIVE result (layer 2) against the ORACLE expectation (layer 1).

This is where the verdict is made, and it reuses the SAME pure judgment the bench sweep uses
(`test_endpoint_sweep._endpoint_allowed`) so "did the endpoint do the thing?" cannot drift between the
in-bench run and the live run.

    expected DENY  + live denied   -> CORRECT
    expected DENY  + live ALLOWED  -> ESCALATION   (a real hole on the live site)
    expected ALLOW + live denied   -> OVER_BLOCK   (we broke it for a legitimate user)
    expected ALLOW + live allowed  -> CORRECT
"""
import json
import os

from tatva_connect.tests.authz.test_endpoint_sweep import _endpoint_allowed

_HERE = os.path.dirname(os.path.abspath(__file__))

G, R, Y, B, X = "\033[32m", "\033[31m", "\033[33m", "\033[1m", "\033[0m"


def _rebuild_body(rec):
	"""The corpus stores a snipped body; restore the shape _endpoint_allowed reads."""
	body = dict(rec.get("body") or {})
	if "_row_count" in body:
		body["message"] = [{}] * body.pop("_row_count")
	return body


def run(corpus_path, expectations_path=None):
	expectations_path = expectations_path or os.path.join(_HERE, "expectations.json")
	with open(expectations_path) as fh:
		expected = {r["case_id"]: r for r in json.load(fh)["cases"]}

	tally = {"CORRECT": 0, "ESCALATION": 0, "OVER_BLOCK": 0, "SKIPPED": 0, "ERROR": 0, "UNJUDGEABLE": 0}
	escalations, overblocks = [], []
	with open(corpus_path) as fh:
		for line in fh:
			rec = json.loads(line)
			exp = expected.get(rec["case_id"], {})
			if rec.get("verdict") in ("SKIPPED", "ERROR"):
				tally[rec["verdict"]] += 1
				continue
			if exp.get("expected_allowed") is None:
				tally["UNJUDGEABLE"] += 1
				continue
			live_allowed = _endpoint_allowed(rec["action"], rec.get("status", 0), _rebuild_body(rec))
			if live_allowed and not exp["expected_allowed"]:
				tally["ESCALATION"] += 1
				escalations.append(rec)
			elif not live_allowed and exp["expected_allowed"]:
				tally["OVER_BLOCK"] += 1
				overblocks.append(rec)
			else:
				tally["CORRECT"] += 1

	_print(tally, escalations, overblocks)
	return {"tally": tally, "escalations": escalations, "over_blocks": overblocks}


def _print(tally, escalations, overblocks):
	print(f"\n{B}══ UAT live vs oracle expectation ══{X}")
	for k, v in tally.items():
		print(f"  {k:<12} {v}")
	for rec in escalations:
		print(f"  {R}ESCALATION{X} {rec['case_id']} — {rec['persona']} got {rec.get('status')} "
		      f"from {rec['method']} (oracle says DENY)")
	for rec in overblocks:
		print(f"  {Y}OVER_BLOCK{X} {rec['case_id']} — {rec['persona']} was refused by {rec['method']} "
		      f"(oracle says ALLOW)")
	verdict = (f"{G}CLEAN — no live escalation{X}" if not escalations
	           else f"{R}{len(escalations)} LIVE ESCALATION(S){X}")
	print(f"{B}══ VERDICT: {verdict}{B} ══{X}\n")
