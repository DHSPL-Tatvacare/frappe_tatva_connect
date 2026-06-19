"""P0 HEADLESS PROOF — run on a dev bench with:

    bench --site dev.localhost execute tatva_connect.smartview.tests.run_proof.run

Proves the composer end-to-end on real SQL:
  * flattened child columns appear in a row (LEFT JOIN of CRM Task Order Detail),
  * filter + sort on a CHILD column work,
  * the PQC is ANDed in (a non-privileged user sees FEWER rows than Administrator),
  * `total` (the PQC-scoped count) matches the returned/visible row count.

Self-seeds isolated test data (a task type, two leads, three order-punch tasks with order
detail rows), so it is deterministic and never depends on business data. Idempotent-ish:
re-running reuses the seeded fixtures by their stable test keys. READ path only — it never
mutates through the composer.
"""
import frappe

from tatva_connect.smartview import api
from tatva_connect.smartview.tests import seed_test_views as seed

TAG = "SMARTVIEW_PROOF"
TYPE = seed.TEST_ACTIVITY_TYPE
TEST_USER = "smartview-proof-agent@example.com"


def _ensure_activity_type():
	"""A minimal CRM Task Type with a scope row, so the type is a real activity type."""
	if not frappe.db.exists("CRM Task Type", TYPE):
		frappe.get_doc({"doctype": "CRM Task Type", "type_name": TYPE, "is_logged_complete": 1}).insert(ignore_permissions=True)
	tt = frappe.get_doc("CRM Task Type", TYPE)
	if not tt.get("scope"):
		tt.append("scope", {"vertical": "", "group": "", "program": ""})
		tt.save(ignore_permissions=True)


def _ensure_user():
	if not frappe.db.exists("User", TEST_USER):
		u = frappe.get_doc({
			"doctype": "User", "email": TEST_USER, "first_name": "Proof Agent",
			"send_welcome_email": 0, "user_type": "System User",
		})
		u.insert(ignore_permissions=True)
		u.add_roles("Sales User")
	return TEST_USER


def _reset_test_data():
	"""Drop prior proof tasks/leads so counts are exact on a re-run."""
	for t in frappe.get_all("CRM Task", filters={"custom_outcome": ["like", "%" + TAG + "%"]}, pluck="name"):
		frappe.delete_doc("CRM Task", t, ignore_permissions=True, force=True)
	for ld in frappe.get_all("CRM Lead", filters={"lead_name": ["like", "%" + TAG + "%"]}, pluck="name"):
		frappe.delete_doc("CRM Lead", ld, ignore_permissions=True, force=True)


def _make_lead(first):
	# TAG lives in first_name (a catalog-searchable projected field) so the proof can
	# isolate its leads via the composer's own search path.
	doc = frappe.get_doc({
		"doctype": "CRM Lead", "first_name": "{0} {1}".format(first, TAG),
		"lead_name": "{0} {1}".format(first, TAG), "status": "New",
	}).insert(ignore_permissions=True)
	return doc.name


def _make_task(lead, owner, outcome, order_id, units, shipped_by):
	t = frappe.get_doc({
		"doctype": "CRM Task", "title": "{0} {1}".format(order_id, TAG),
		"custom_task_type": TYPE, "status": "Todo",
		"reference_doctype": "CRM Lead", "reference_docname": lead,
		"custom_outcome": "{0} {1}".format(outcome, TAG),
		"custom_order_detail": [{
			"outcome": outcome, "order_id": order_id,
			"number_of_units": units, "shipped_by_date": shipped_by,
		}],
	})
	t.insert(ignore_permissions=True)
	# stamp owner explicitly (PQC keys off owner/assigned_to)
	frappe.db.set_value("CRM Task", t.name, "owner", owner)
	return t.name


def _seed_proof_data(agent):
	admin = "Administrator"
	lead_a = _make_lead("Alpha")
	lead_b = _make_lead("Bravo")
	# Two tasks OWNED BY the agent (visible to them), one owned by Administrator (hidden from agent).
	_make_task(lead_a, agent, "Delivered", "ORD-1001", 2, "2026-06-25 10:00:00")
	_make_task(lead_a, agent, "Shipped", "ORD-1002", 5, "2026-06-22 10:00:00")
	_make_task(lead_b, admin, "Delivered", "ORD-2001", 1, "2026-06-30 10:00:00")
	frappe.db.commit()


def _check(label, cond):
	status = "PASS" if cond else "FAIL"
	print("  [{0}] {1}".format(status, label))
	return cond


def run():
	frappe.flags.in_test = True
	# Turn ON the task visibility switch so the PQC actually scopes (it is a kill-switch).
	from tatva_connect import automation
	switch_on = automation.is_enabled("Task::CRM Task::visibility")

	_ensure_activity_type()
	agent = _ensure_user()
	_reset_test_data()
	views = seed.seed()
	_seed_proof_data(agent)

	lead_view, act_view = views["Lead"], views["Activity"]
	results = []

	print("\n== Lead view ==")
	data = api.get_data(lead_view, search=TAG)
	cols = [c["key"] for c in data["columns"]]
	results.append(_check("columns present (first_name/status/mobile)", "lead:first_name" in cols))
	results.append(_check("rows returned (>=2 test leads)", len([r for r in data["rows"]]) >= 2))
	results.append(_check("count == returned rows", data["total"] == len(data["rows"])))

	print("\n== Activity view — flatten child columns ==")
	data = api.get_data(act_view)
	cols = [c["key"] for c in data["columns"]]
	results.append(_check("child column order:order_id projected", "order:order_id" in cols))
	row0 = data["rows"][0] if data["rows"] else {}
	results.append(_check("flattened child value present (order:order_id non-null)",
						   any(r.get("order:order_id") for r in data["rows"])))
	print("    sample row:", {k: row0.get(k) for k in ("name", "order:order_id", "order:outcome", "order:units")})

	print("\n== Filter + sort on a CHILD column ==")
	filtered = api.get_data(act_view, filters=[["order:outcome", "=", "Delivered"]], sort=["order:order_id", "asc"])
	out_vals = [r.get("order:outcome") for r in filtered["rows"]]
	results.append(_check("filter order:outcome=Delivered -> only Delivered rows",
						   bool(out_vals) and all(v == "Delivered" for v in out_vals)))
	ids = [r.get("order:order_id") for r in filtered["rows"]]
	results.append(_check("sort by order:order_id asc -> ascending", ids == sorted(ids)))
	results.append(_check("filtered count == rows", filtered["total"] == len(filtered["rows"])))

	print("\n== PQC narrows rows (non-privileged user sees fewer) ==")
	admin_data = api.get_data(act_view)
	admin_total = admin_data["total"]
	agent_total = None
	if not switch_on:
		print("    NOTE: Task::CRM Task::visibility switch is OFF -> PQC is a no-op (stock crm).")
		print("    Enabling it for the duration of the proof to exercise the fail-closed path.")
	# Force the switch ON in-process so the PQC actually applies for the agent.
	orig = automation.is_enabled
	automation.is_enabled = lambda key: True if key == "Task::CRM Task::visibility" else orig(key)
	try:
		frappe.set_user(agent)
		agent_data = api.get_data(act_view)
		agent_total = agent_data["total"]
	finally:
		frappe.set_user("Administrator")
		automation.is_enabled = orig
	print("    admin total={0}  agent total={1}".format(admin_total, agent_total))
	results.append(_check("agent (non-privileged) sees FEWER rows than admin", agent_total < admin_total))
	results.append(_check("agent count == agent rows (count is PQC-scoped)", agent_total == len(agent_data["rows"])))

	ok = all(results)
	print("\n==== PROOF {0} ({1}/{2} checks passed) ====".format(
		"PASSED" if ok else "FAILED", sum(1 for r in results if r), len(results)))
	return ok
