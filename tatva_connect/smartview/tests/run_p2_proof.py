"""P2 HEADLESS PROOF — run on a dev bench with:

    bench --site dev.localhost execute tatva_connect.smartview.tests.run_p2_proof.run

Proves the authoring write path (upsert_view / delete_view) + the interactive `columns`
override end-to-end, on real records:
  * a non-operator can CREATE a personal view (owned, is_standard=0) and it shows in their tabs,
  * UPDATE rewrites label/columns/predicate in place,
  * the catalog allowlist REJECTS a non-catalog column and a non-filterable predicate field,
  * a saved predicate round-trips through get_data (filters the rows),
  * the interactive `columns` override projects ONLY the requested catalog keys,
  * owner-scope: user B cannot edit/delete user A's view (PermissionError),
  * a non-operator asking for is_standard gets a PERSONAL view (no privilege escalation),
  * the per-owner view CAP is enforced on create,
  * delete is owner-scoped.

Self-seeds two Sales users + a few tagged leads; idempotent-ish (cleans its own rows first).
"""
import frappe

from tatva_connect.smartview import api

TAG = "SMARTVIEW_P2"
USER_A = "smartview-p2-a@example.com"
USER_B = "smartview-p2-b@example.com"


def _ensure_user(email):
	if not frappe.db.exists("User", email):
		u = frappe.get_doc({
			"doctype": "User", "email": email, "first_name": email.split("@")[0],
			"send_welcome_email": 0, "user_type": "System User",
		})
		u.insert(ignore_permissions=True)
		u.add_roles("Sales User")
	return email


def _reset():
	for v in frappe.get_all("CRM Smart View", filters={"label": ["like", "%" + TAG + "%"]}, pluck="name"):
		frappe.delete_doc("CRM Smart View", v, ignore_permissions=True, force=True)
	for ld in frappe.get_all("CRM Lead", filters={"lead_name": ["like", "%" + TAG + "%"]}, pluck="name"):
		frappe.delete_doc("CRM Lead", ld, ignore_permissions=True, force=True)
	frappe.db.commit()


def _make_lead(first, status):
	return frappe.get_doc({
		"doctype": "CRM Lead", "first_name": "{0} {1}".format(first, TAG),
		"lead_name": "{0} {1}".format(first, TAG), "status": status,
	}).insert(ignore_permissions=True).name


def _check(label, cond):
	print("  [{0}] {1}".format("PASS" if cond else "FAIL", label))
	return cond


def _throws(label, fn):
	"""PASS iff fn() raises (a fail-closed rejection)."""
	try:
		fn()
		print("  [FAIL] {0} (no error raised)".format(label))
		return False
	except Exception as e:
		print("  [PASS] {0} -> {1}".format(label, type(e).__name__))
		return True


def run():
	frappe.flags.in_test = True
	a, b = _ensure_user(USER_A), _ensure_user(USER_B)
	_reset()
	_make_lead("Alpha", "New")
	_make_lead("Bravo", "Open")
	frappe.db.commit()

	# catalog keys we can safely reference (real Lead catalog rows)
	cat = api.field_catalog("Lead")
	cat_keys = [c["field_key"] for c in cat]
	filterable = [c["field_key"] for c in cat if c["filterable"]]
	name_key = next((k for k in cat_keys if k.endswith(":first_name") or k.endswith("first_name")), cat_keys[0])
	status_key = next((k for k in filterable if k.endswith("status")), filterable[0])
	results = []

	print("\n== Create (non-operator owns a personal view) ==")
	frappe.set_user(a)
	try:
		tab = api.upsert_view({
			"label": "Mine " + TAG, "base_object": "Lead",
			"columns": [name_key, status_key],
			"predicate": {"op": "and", "conditions": [{"field": status_key, "operator": "=", "value": "Open"}]},
		})
		vname = tab["name"]
		doc = frappe.get_doc("CRM Smart View", vname)
		results.append(_check("created view owned by A, is_standard=0", doc.owner_user == a and not doc.is_standard))
		mine = [t["name"] for t in api.get_smart_views()]
		results.append(_check("appears in A's get_smart_views", vname in mine))

		print("\n== Predicate round-trips through get_data ==")
		data = api.get_data(vname, search=TAG)
		statuses = [r.get(status_key) for r in data["rows"]]
		results.append(_check("saved predicate status=Open -> only Open rows", bool(statuses) and all(s == "Open" for s in statuses)))

		print("\n== Interactive columns override (catalog-bounded) ==")
		one = api.get_data(vname, columns=[status_key], search=TAG)
		keys = [c["key"] for c in one["columns"]]
		results.append(_check("override projects ONLY requested key", keys == [status_key]))
		bogus_dropped = api.get_data(vname, columns=[status_key, "totally:bogus"], search=TAG)
		results.append(_check("unknown override key dropped (no error)", [c["key"] for c in bogus_dropped["columns"]] == [status_key]))

		print("\n== Allowlist rejects bad writes (fail-closed) ==")
		results.append(_throws("upsert with non-catalog column throws",
							   lambda: api.upsert_view({"label": "Bad " + TAG, "base_object": "Lead", "columns": ["totally:bogus"]})))
		results.append(_throws("upsert with unknown predicate field throws",
							   lambda: api.upsert_view({"label": "Bad2 " + TAG, "base_object": "Lead",
													   "predicate": {"op": "and", "conditions": [{"field": "totally:bogus", "operator": "=", "value": 1}]}})))
		results.append(_throws("Activity view without activity_type throws",
							   lambda: api.upsert_view({"label": "Bad3 " + TAG, "base_object": "Activity"})))

		print("\n== Update in place ==")
		api.upsert_view({"name": vname, "label": "Mine Renamed " + TAG, "base_object": "Lead", "columns": [name_key]})
		results.append(_check("label updated", frappe.get_doc("CRM Smart View", vname).label == "Mine Renamed " + TAG))

		print("\n== No privilege escalation (non-operator can't make a standard view) ==")
		esc = api.upsert_view({"label": "TryStd " + TAG, "base_object": "Lead", "is_standard": 1})
		results.append(_check("is_standard request from non-operator -> personal view", not frappe.get_doc("CRM Smart View", esc["name"]).is_standard))
	finally:
		frappe.set_user("Administrator")

	print("\n== Owner-scope: B cannot edit/delete A's view ==")
	frappe.set_user(b)
	try:
		results.append(_throws("B editing A's view throws",
							   lambda: api.upsert_view({"name": vname, "label": "Hijack " + TAG, "base_object": "Lead"})))
		results.append(_throws("B deleting A's view throws", lambda: api.delete_view(vname)))
	finally:
		frappe.set_user("Administrator")

	print("\n== Per-owner cap ==")
	frappe.set_user(a)
	try:
		# A already owns a few; top up to the cap then prove the (cap+1)th is refused.
		owned = frappe.db.count("CRM Smart View", {"owner_user": a, "is_standard": 0})
		for i in range(owned, api.OWNER_VIEW_CAP):
			api.upsert_view({"label": "Cap{0} {1}".format(i, TAG), "base_object": "Lead"})
		results.append(_check("A now owns exactly the cap",
							 frappe.db.count("CRM Smart View", {"owner_user": a, "is_standard": 0}) == api.OWNER_VIEW_CAP))
		results.append(_throws("creating one past the cap throws",
							   lambda: api.upsert_view({"label": "Over " + TAG, "base_object": "Lead"})))

		print("\n== Owner delete ==")
		res = api.delete_view(vname)
		results.append(_check("A deletes own view", res.get("deleted") == vname and not frappe.db.exists("CRM Smart View", vname)))
	finally:
		frappe.set_user("Administrator")
		_reset()

	ok = all(results)
	print("\n==== P2 PROOF {0} ({1}/{2} checks passed) ====".format(
		"PASSED" if ok else "FAILED", sum(1 for r in results if r), len(results)))
	return ok
