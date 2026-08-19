# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""Proof, on live data, that the ACL index returns EXACTLY the rows crm's rule returns — per user.

WHY THIS EXISTS AND NOT JUST A UNIT TEST. The switch replaces the function that decides who may read a
patient record. A fixture-sized test proves the shape; only the real table proves the SET. This runs
read-only against production, compares the two rules user by user, and reports any divergence by name.
Nothing is armed until it reports zero.

DIVERGENCE IS DIRECTIONAL AND BOTH DIRECTIONS MATTER. `missing` is a rep who would LOSE records they can
see today — an outage. `extra` is a rep who would GAIN records — a leak, and the one that gets people
fired. They are reported separately because they are not the same kind of wrong.

It also times both, because the whole point was speed and a claim of speed should carry a number.

Run:
    bench --site <site> execute tatva_connect.access.record_access_audit.audit
    bench --site <site> execute tatva_connect.access.record_access_audit.audit --kwargs "{'doctype':'CRM Deal'}"
"""
import time

import frappe
from crm.permissions.org_hierarchy import _permission_query_conditions

from tatva_connect.access import record_access


def _crm_condition(doctype: str, user: str) -> str:
	"""crm's own restrictive WHERE, built the way its public function builds it — unwrapped by install()."""
	cond = _permission_query_conditions(user, doctype)
	if not cond:
		return ""
	return cond.get_sql(quote_char="`", secondary_quote_char="'", with_namespace=True)


def _names(doctype: str, where: str) -> tuple[set[str], float]:
	started = time.time()
	rows = frappe.db.sql(f"select `name` from `tab{doctype}` where {where}")  # sqli-ok: both conditions are framework-built, no caller value reaches this string
	return {r[0] for r in rows}, round((time.time() - started) * 1000, 1)


def audit(doctype: str = "CRM Lead", users: list | None = None) -> dict:
	"""Compare the two rules for every user holding a grant. Read-only; changes nothing."""
	if users is None:
		users = frappe.get_all(
			record_access.DOCTYPE, filters={"reference_doctype": doctype}, pluck="user", distinct=True
		)
	report = {"doctype": doctype, "checked": 0, "skipped_unrestricted": 0, "diverged": [], "timing": []}

	for user in sorted(set(users)):
		crm_where = _crm_condition(doctype, user)
		if not crm_where:
			report["skipped_unrestricted"] += 1  # crm says unrestricted; install() never replaces that
			continue
		acl_where = record_access.condition(doctype, user, force=True)
		if not acl_where:
			report["diverged"].append({"user": user, "reason": "acl declined to answer"})
			continue

		old, old_ms = _names(doctype, crm_where)
		new, new_ms = _names(doctype, acl_where)
		report["checked"] += 1
		report["timing"].append({"user": user, "crm_ms": old_ms, "acl_ms": new_ms, "rows": len(old)})
		if old != new:
			report["diverged"].append({
				"user": user,
				"missing": sorted(old - new)[:20],   # would LOSE access — an outage
				"extra": sorted(new - old)[:20],     # would GAIN access — a leak
				"missing_count": len(old - new),
				"extra_count": len(new - old),
			})

	timings = report["timing"]
	if timings:
		report["summary"] = {
			"users": len(timings),
			"crm_ms_total": round(sum(t["crm_ms"] for t in timings), 1),
			"acl_ms_total": round(sum(t["acl_ms"] for t in timings), 1),
			"crm_ms_worst": max(t["crm_ms"] for t in timings),
			"acl_ms_worst": max(t["acl_ms"] for t in timings),
		}
	report["timing"] = sorted(timings, key=lambda t: -t["crm_ms"])[:10]
	report["verdict"] = "SAFE TO ARM" if not report["diverged"] else "DO NOT ARM"
	print(frappe.as_json(report))
	return report
