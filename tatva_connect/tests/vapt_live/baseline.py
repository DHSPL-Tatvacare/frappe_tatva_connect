# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""LAYER 1 — the ORACLE runs here, on the bench, and exports its ANSWER.

The differential oracle (`actual ⊆ native`) cannot run off a bench: it asks Frappe itself what the
native permission stack would allow. So we run it locally, once, and export a portable expectation per
case. The live HTTP run (layer 2) never needs an oracle — it fires and records; layer 3 diffs.

What travels is a ROLE-semantic expectation, never a record id:
    (persona, endpoint_key, doctype, action, target_relation) -> expected_allowed
so it stays valid on a UAT whose rows are different but whose ROLES are the same.

Run on the bench:
    bench --site dev.localhost execute tatva_connect.tests.vapt_live.baseline.build
"""
import json
import os

import frappe

from tatva_connect.tests.authz import oracle, roster
from tatva_connect.tests.authz.registry import cases

_HERE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(_HERE, "expectations.json")

# A hostile persona attacks a row it does not own; that is the only relation the sweep generates.
TARGET_RELATION = "foreign"


def _target_for(doctype):
	"""A real foreign-owned row of `doctype` on THIS bench, so the oracle judges a concrete object."""
	if not doctype or not frappe.db.exists("DocType", doctype):
		return None
	return frappe.db.get_value(doctype, {}, "name")


def build(out_path=None):
	"""Generate every HTTP case, ask the native oracle its verdict, and export the expectation table."""
	out_path = out_path or OUT_PATH
	generated = cases.generate_http_cases()
	rows, skipped = [], 0
	for c in generated:
		obj_id = _target_for(c.doctype)
		if c.doctype and not obj_id:
			skipped += 1  # no target on this bench -> cannot judge; recorded, never silently dropped
			rows.append({
				"case_id": c.id, "persona": c.principal, "endpoint_key": c.endpoint_key,
				"method": c.method, "http": c.http, "action": c.action, "doctype": c.doctype,
				"target_relation": TARGET_RELATION, "expected_allowed": None, "reason": "no-target-on-baseline",
			})
			continue
		expected = oracle.native_http_verdict(roster.email(c.principal), c.action, c.doctype, obj_id)
		rows.append({
			"case_id": c.id, "persona": c.principal, "endpoint_key": c.endpoint_key,
			"method": c.method, "http": c.http, "action": c.action, "doctype": c.doctype,
			"target_relation": TARGET_RELATION, "expected_allowed": bool(expected), "reason": "",
		})
	payload = {"generated": len(rows), "unjudgeable": skipped, "cases": rows}
	with open(out_path, "w") as fh:
		json.dump(payload, fh, indent=1)
	print(f"[baseline] cases={len(rows)} unjudgeable={skipped} -> {out_path}")
	return payload
