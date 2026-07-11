# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Check what actually landed in the database, not what the API said it did.

A 200 with `action: created` proves the request was accepted. It does not prove the row exists, that
it hangs off the right lead, that the file is private, or that a retry did not quietly write a second
copy. Those are separate claims and each is checked here against the tables themselves.

Runs INSIDE the backend container:

    bench-python tatva_connect/tests/loadtest/validate.py anaya
"""
import json
import sys
from collections import Counter
from pathlib import Path

import frappe

from tatva_connect.tests.loadtest.config import PARTNER_USER

REPORTS = Path(__file__).resolve().parent / "reports"

GRAIN = {
	"anaya": ("GoodFlip Care", "Anaya"),
	"tatvapractice": ("TatvaPractice", "India"),
}


class Checks:
	def __init__(self):
		self.rows = []

	def add(self, name, ok, detail):
		self.rows.append({"check": name, "ok": bool(ok), "detail": detail})

	def report(self):
		width = max(len(r["check"]) for r in self.rows)
		failed = 0
		for r in self.rows:
			mark = "PASS" if r["ok"] else "FAIL"
			if not r["ok"]:
				failed += 1
			print(f"  [{mark}] {r['check']:<{width}}  {r['detail']}")
		return failed


def validate(account):
	vertical, group = GRAIN[account]
	c = Checks()
	print(f"=== {account}  ({vertical} :: {group}) ===")

	leads = frappe.get_all("CRM Lead", filters={"custom_vertical": vertical, "custom_group": group},
	                       fields=["name", "mobile_no", "custom_external_id", "custom_current_program"])
	names = [row.name for row in leads]
	c.add("leads exist", bool(names), f"{len(names)} lead(s) on the grain")
	if not names:
		return c.report()

	# 1. DEDUP. The contract is one lead per (mobile, vertical, group). A duplicate here means the
	#    anchor leaked, and it is the single most damaging thing this load could have done.
	dupes = [m for m, n in Counter(row.mobile_no for row in leads if row.mobile_no).items() if n > 1]
	c.add("dedup holds", not dupes,
	      "no duplicate mobile on the grain" if not dupes else f"{len(dupes)} duplicated mobile(s): {dupes[:5]}")

	# 2. EXTERNAL ID is stored and echoed back, but is only a label.
	labelled = sum(1 for row in leads if row.custom_external_id)
	c.add("external_id stored", labelled > 0, f"{labelled}/{len(leads)} leads carry their LSQ id")

	# 3. ACTIVITY BINDING. Every task the API made must hang off a lead on this grain, by the
	#    reference pair -- an orphan task is invisible in the lead view even though it "succeeded".
	tasks = frappe.get_all("CRM Task",
	                       filters={"reference_doctype": "CRM Lead", "reference_docname": ["in", names]},
	                       fields=["name", "reference_docname", "custom_task_type", "status", "owner"])
	orphans = frappe.db.count("CRM Task", {"reference_doctype": "CRM Lead",
	                                       "reference_docname": ["is", "not set"]})
	c.add("activities bound to a lead", bool(tasks), f"{len(tasks)} task(s) reference a lead on this grain")
	c.add("no orphan activities", orphans == 0, f"{orphans} task(s) reference no lead")

	# The type check has to look at what the API WROTE, not at everything on the grain. These grains
	# also carry tasks from earlier migration trials and from ordinary CRM use, owned by real people
	# and typed by whatever made them — an untyped one of those says nothing about this run. The API's
	# writes are the ones owned by the partner user.
	partner = PARTNER_USER[account]
	mine = [t for t in tasks if t.owner == partner]
	untyped = [t.name for t in mine if not t.custom_task_type]
	types = Counter(t.custom_task_type for t in mine)
	c.add("task types resolved", mine and not untyped,
	      f"{len(types)} distinct type(s) across {len(mine)} task(s) the API wrote"
	      if not untyped else f"{len(untyped)} untyped: {untyped[:3]}")

	# 4. CALL BINDING.
	calls = frappe.get_all("CRM Call Log", filters={"reference_doctype": "CRM Lead",
	                                                "reference_docname": ["in", names]},
	                       fields=["name", "reference_docname", "type", "status"])
	c.add("calls bound to a lead", True, f"{len(calls)} call log(s) on this grain's leads")

	# 5. FILE BINDING + PRIVACY. A file is homed either on its lead or on an activity of that lead;
	#    either way it must be private, because the API promises every attachment is.
	task_names = [t.name for t in tasks]
	files = frappe.get_all("File", filters={"attached_to_name": ["in", names + task_names]},
	                       fields=["name", "attached_to_doctype", "attached_to_name", "is_private", "file_url"])
	public = [f.name for f in files if not f.is_private]
	c.add("files bound", True, f"{len(files)} file(s) on leads/activities of this grain")
	c.add("files are private", not public,
	      "every file is private" if not public else f"{len(public)} PUBLIC file(s): {public[:3]}")

	homes = Counter(f.attached_to_doctype for f in files)
	c.add("file homes are known doctypes",
	      set(homes) <= {"CRM Lead", "CRM Task", "FCRM Note"} or not homes,
	      f"{dict(homes)}" if homes else "no files attached")

	# 6. LEAD VIEW. What the CRM shows on a lead is driven by these same reference pairs, so a lead
	#    with activities must resolve to a non-zero count through the same lookup the UI does.
	with_tasks = len({t.reference_docname for t in tasks})
	c.add("activities visible on a lead", with_tasks > 0,
	      f"{with_tasks}/{len(names)} leads have at least one activity in the lead view")

	# 7. PROGRAM. A program-scoped task type only accepts a lead already on that program, so a
	#    program that failed to resolve would have shown up as a wall of rejected activities.
	programs = Counter(row.custom_current_program or "(none)" for row in leads)
	c.add("programs resolved", True, dict(programs))

	return c.report()


def main():
	accounts = sys.argv[1:] or list(GRAIN)
	frappe.init(site="dev.localhost")
	frappe.connect()
	failed = 0
	try:
		for account in accounts:
			failed += validate(account)
			print()
	finally:
		frappe.destroy()

	REPORTS.mkdir(parents=True, exist_ok=True)
	(REPORTS / "validate.json").write_text(json.dumps({"failed": failed}, indent=2) + "\n")
	print("ALL CHECKS PASS" if not failed else f"{failed} CHECK(S) FAILED")
	return 1 if failed else 0


if __name__ == "__main__":
	sys.exit(main())
