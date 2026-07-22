# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Layer 2 + 3 — the API-layer deny-sweep over REAL HTTP (the VAPT class).

Standalone runner (the committed / Playwright lifecycle, README.md section 8): a browser/HTTP request
runs on a separate connection and cannot see an uncommitted txn, so this seeds with commit=True and
tears down by tag. It:
  1. asserts comms-off (never a real send),
  2. seeds the roster (+ per-persona tokens) and ONE foreign-owned instance per sensitive doctype,
  3. generates the B1..B5 cases from the registry primitives (never hand-listed),
  4. fires each as its hostile persona via the HTTP engine,
  5. judges every response against the ORACLE (native_http_verdict) — an endpoint that returns/does
     MORE than native allows is an ESCALATION (a live VAPT-class hole),
  6. asserts the generator covers every known VAPT finding (recall), then tears down.

Invoke from tcsec.py or: bench --site <site> execute
tatva_connect.tests.authz.test_endpoint_sweep.run
"""
import os
import time

import frappe

from tatva_connect.tests.authz import generator, http_engine, oracle, roster
from tatva_connect.tests.authz.comms import assert_comms_off
from tatva_connect.tests.authz.registry import cases, endpoints
from tatva_connect.tests.authz.vapt import findings

TAG = generator.TAG

# Minimal foreign-owned instances (planted as Administrator, tagged) so a read/write/delete case has a
# REAL id owned by someone else — proving object-level authorization, not a 404 on a missing id. CRM
# Lead/Task are already seeded by generator; the rest are minimal, best-effort (a doctype we cannot seed
# cleanly is skipped WITH a log, never silently — its cases become 'skip-no-target').
_FOREIGN_SEED = {
	"Contact": {"first_name": f"{TAG}-contact"},
	# reference_name is filled at seed time with the foreign-owned lead (a Comment validates its parent
	# ref, so a null reference_name errors) — without this the P1 vapt-01 Comment IDOR never executes.
	"Comment": {"comment_type": "Comment", "reference_doctype": "CRM Lead", "content": f"{TAG}-comment"},
	"ToDo": {"description": f"{TAG}-todo", "allocated_to": "Administrator"},
	# CRM Organization is seeded first so CRM Deal (below) can Link a REAL org; both are tagged + torn down.
	"CRM Organization": {"organization_name": f"{TAG}-org"},
	"CRM Deal": {},  # special-cased in _seed_foreign_objects (org Link + reqd status resolved live)
	"HD Ticket": {"subject": f"{TAG}-ticket"},
	"CRM Call Log": {"id": f"{TAG}-call", "from": "+910000000000", "to": "+910000000001",
	                 "type": "Incoming", "status": "Completed"},
	"Assignment Rule": {"name": f"{TAG}-arule", "document_type": "ToDo", "assign_condition": "1",
	                    "rule": "Round Robin", "users": [{"user": "Administrator"}]},
	# a PRIVATE File is the whole point of the B5 private-file IDOR (vapt-12) — plant one owned by
	# Administrator so a hostile read is a real object-level test, not a 404.
	"File": {"file_name": f"{TAG}-secret.txt", "is_private": 1, "content": f"{TAG}-file-body"},
	"FCRM Note": {"title": f"{TAG}-note", "content": f"{TAG}-note-body"},
	# VAPT Jul: a real exercise carrying a hidden answer key, so the LMS cases have a foreign-owned target.
	"LMS Programming Exercise": {"title": f"{TAG}-exercise", "problem_statement": "x", "language": "Python",
	                             "test_cases": [{"input": f"{TAG}-in", "expected_output": f"{TAG}-out"}]},
}
_ENDPOINT_BY_KEY = {e.key: e for e in (*endpoints.GENERIC_ENDPOINTS, *endpoints.APP_ENDPOINTS)}


def _seed_foreign_objects():
	"""Return {doctype: name} of a foreign-owned instance per sensitive doctype. Best-effort + logged."""
	owned = {}
	lead = frappe.db.get_value("CRM Lead", {"lead_name": ["like", TAG + "%"]}, "name")
	if lead:
		owned["CRM Lead"] = lead
		owned["CRM Task"] = frappe.db.get_value("CRM Task", {"reference_docname": lead}, "name")
	for doctype, payload in _FOREIGN_SEED.items():
		existing = frappe.db.get_value(doctype, {"name": ["like", TAG + "%"]}, "name")
		if existing:  # name-tagged doctypes (CRM Call Log / Assignment Rule) — reuse, never duplicate
			owned[doctype] = existing
			continue
		payload = dict(payload)
		if doctype == "Comment" and lead:
			payload["reference_name"] = lead  # bind the Comment to a real foreign-owned parent
		if doctype == "CRM Deal":  # resolve the org Link (seeded above) + the reqd status Link, live
			st = (frappe.get_all("CRM Deal Status", {"type": ["!=", "Lost"]}, pluck="name", limit=1)
				or frappe.get_all("CRM Deal Status", pluck="name", limit=1))  # a Lost status demands a reason
			payload = {"organization": owned.get("CRM Organization"), "status": st[0] if st else None}
		try:
			doc = frappe.get_doc({"doctype": doctype, **payload})
			doc.insert(ignore_permissions=True)
			owned[doctype] = doc.name
		except Exception as e:
			print(f"  [seed] skip {doctype}: {str(e)[:90]}")
	return owned


def _teardown_foreign_objects():
	"""Delete every foreign-owned instance _seed_foreign_objects planted — generator.teardown() only
	knows the roster + leads/tasks, so without this the Contact/Comment/ToDo/HD Ticket/Call Log plants
	accumulate every run. The tag field per doctype is derived from the seed payload (the one value that
	carries TAG), so adding a doctype to _FOREIGN_SEED auto-extends teardown. Best-effort + logged."""
	for doctype, payload in _FOREIGN_SEED.items():
		field = next((k for k, v in payload.items() if isinstance(v, str) and v.startswith(TAG)), None)
		if not field:
			continue
		for name in frappe.get_all(doctype, {field: ["like", TAG + "%"]}, pluck="name"):
			try:
				frappe.delete_doc(doctype, name, ignore_permissions=True, force=True)
			except Exception as e:
				print(f"  [teardown] skip {doctype} {name}: {str(e)[:80]}")


def _teardown_all():
	"""Full committed-lifecycle cleanup. The sweep fires real writes on the gunicorn connection while
	THIS process holds a read snapshot from early in the loop; deleting a persona User then updates
	tabContact and MariaDB raises 1020 (row changed since the snapshot). rollback() drops that stale
	snapshot so the delete reads fresh; a short backoff+retry absorbs any worker still settling."""
	for attempt in range(1, 6):
		frappe.db.rollback()  # fresh read snapshot — the actual fix for the 1020 race
		try:
			_teardown_foreign_objects()
			generator.teardown()
			return
		except Exception:  # authz-ok: cleanup-only retry; concurrent-commit vs teardown race (1020, class varies)
			if attempt == 5:
				raise
			time.sleep(attempt * 2)


def _list_row_count(msg):
	"""Rows a list/report endpoint actually returned, looking THROUGH the metadata envelope: the
	columns/keys wrapper is NOT data. hd-list-data / crm-get-data -> {data:[...], columns, total_count};
	reportview.get -> {keys, values:[...]}; frappe.client.get_list -> a plain list."""
	if isinstance(msg, list):
		return len(msg)
	if isinstance(msg, dict):
		if isinstance(msg.get("data"), list):
			return len(msg["data"])
		if isinstance(msg.get("values"), list):
			return len(msg["values"])
	return 0


def _endpoint_allowed(action, code, body):
	"""Did the endpoint DO the thing (return data / succeed)? True = allowed, False = denied."""
	if not (200 <= code < 300):
		return False  # 401/403/404/400/500 = did not succeed = not an escalation
	if isinstance(body, dict) and (body.get("exc_type") or body.get("_server_messages")):
		return False  # framework-level error in a 200 envelope
	msg = body.get("message") if isinstance(body, dict) else None
	if action == "list":
		return _list_row_count(msg) > 0  # rows, never the columns/keys metadata wrapper
	if action in ("read", "info"):
		return bool(msg) and msg not in ([], {}, "")
	return True  # write / create / delete succeeded


def _escalates(action, code, body, native_ok):
	"""The ONE escalation judgment shared by the live sweep and the self-validation mutation
	(mutation._endpoint_escalation): the endpoint DID the thing AND native would DENY it. True = an
	escalation (a live VAPT-class hole). Keeping it a single pure function means the mutation proves the
	exact predicate the sweep runs, never a re-implemented copy."""
	return _endpoint_allowed(action, code, body) and not native_ok


def run(base=None, host="dev.localhost"):
	# The sweep runs IN the bench (it needs frappe for seed/oracle/teardown), so it must hit the site by
	# its INTERNAL url, not the host port map. Override via AUTHZ_HTTP_BASE in CI; default = local gunicorn.
	base = base or os.environ.get("AUTHZ_HTTP_BASE") or "http://localhost:8000"
	assert_comms_off()
	generator.seed(commit=True)
	foreign = _seed_foreign_objects()
	frappe.db.commit()
	eng = http_engine.HttpEngine(http_engine.load_creds(generator.creds_path()), base, host)
	generated = cases.generate_http_cases()

	escalations, skipped, results = [], 0, []
	for c in generated:
		spec = _ENDPOINT_BY_KEY[c.endpoint_key]
		obj_id = foreign.get(c.doctype) if c.doctype else None
		if c.action in ("read", "write", "delete") and c.doctype and not obj_id:
			skipped += 1
			continue
		params = endpoints.build_params(spec, c.doctype, obj_id or "MISSING")
		code, body = eng.call(c.principal, spec.method, spec.http, params)
		allowed = _endpoint_allowed(c.action, code, body)
		native_ok = oracle.native_http_verdict(roster.email(c.principal), c.action, c.doctype, obj_id)
		if _escalates(c.action, code, body, native_ok):
			rec = {"case": c.id, "code": code, "method": spec.method,
			       "endpoint_key": c.endpoint_key, "doctype": c.doctype,
			       "benign": findings.is_benign_residual(c.endpoint_key, c.doctype)}
			escalations.append(rec)
		results.append((c.id, code, allowed, native_ok))

	uncovered = findings.uncovered(generated)
	real = [e for e in escalations if not e["benign"]]
	benign = [e for e in escalations if e["benign"]]
	report = {"generated": len(generated), "skipped_no_target": skipped,
	          "escalations": real, "benign_residuals": benign, "vapt_uncovered": uncovered}
	# Print the verdict BEFORE teardown — teardown races the gunicorn connection and can throw, and the
	# escalation list is the whole point of the run; it must never be lost to a cleanup flake.
	print(f"[endpoint-sweep] cases={len(generated)} skipped={skipped} "
	      f"escalations={len(real)} benign_residuals={len(benign)} vapt_uncovered={len(uncovered)}")
	for e in real:
		print(f"  ESCALATION {e['case']} (HTTP {e['code']}) {e['method']}")
	for e in benign:
		print(f"  benign-residual {e['case']} ({e['endpoint_key']}/{e['doctype']}) — allowlisted")
	if uncovered:
		print(f"  VAPT COVERAGE GAP: {uncovered}")
	assert_residuals_benign(eng)  # re-prove the allowlist is still non-sensitive (trap check)
	_teardown_all()
	return report


def assert_residuals_benign(eng):
	"""Re-prove, every run, that the allowlisted File-list residual is still NON-SENSITIVE: a foreign
	PRIVATE file (owned by Administrator, attached to nothing) must appear in NONE of the three list
	endpoints and be unreadable to a non-privileged persona. If it ever leaks, the allowlist is wrong and
	the run FAILS — so 'benign' can never quietly rot into a real hole. Guards findings.BENIGN_RESIDUALS."""
	trap = frappe.get_doc({"doctype": "File", "file_name": f"{TAG}-residual-trap.txt",
	                       "is_private": 1, "content": f"{TAG}-trap-secret"}).insert(ignore_permissions=True)
	frappe.db.commit()
	try:
		leaked = []
		for persona in ("no_role", "default_user"):
			for method in ("frappe.client.get_list", "frappe.desk.reportview.get", "helpdesk.api.doc.get_list_data"):
				_, body = eng.call(persona, method, "POST",
				                   {"doctype": "File", "fields": ["name"], "limit_page_length": 2000})
				msg = body.get("message") if isinstance(body, dict) else None
				rows = msg if isinstance(msg, list) else ((msg.get("data") or msg.get("values") or []) if isinstance(msg, dict) else [])
				if any(trap.name in str(r) for r in rows):
					leaked.append(f"{persona}/{method}")
			_, body = eng.call(persona, "frappe.client.get", "GET", {"doctype": "File", "name": trap.name})
			if isinstance(body, dict) and not body.get("exc_type") and (body.get("message") or {}).get("file_url"):
				leaked.append(f"{persona}/client.get(bytes)")
		if leaked:
			raise AssertionError(f"BENIGN RESIDUAL BROKEN — foreign private File leaked to: {leaked}. "
			                     "findings.BENIGN_RESIDUALS is no longer safe.")
	finally:
		frappe.delete_doc("File", trap.name, force=True, ignore_permissions=True)
		frappe.db.commit()


def gate(base="http://localhost:8000", host="dev.localhost"):
	"""CI/gate entry: run the sweep and FAIL (raise) on any escalation or VAPT coverage gap, so
	`bench execute tatva_connect.tests.authz.test_endpoint_sweep.gate` exits non-zero and blocks the
	pipeline. Default base is the in-container gunicorn (localhost:8000); pass base for a host run.
	Kept separate from run() so run() stays a plain report producer for programmatic/manual use."""
	report = run(base, host)
	problems = []
	if report["escalations"]:
		problems.append(f"{len(report['escalations'])} escalation(s): "
		                + ", ".join(e["case"] for e in report["escalations"]))
	if report["vapt_uncovered"]:
		problems.append(f"{len(report['vapt_uncovered'])} uncovered VAPT finding(s): "
		                + ", ".join(report["vapt_uncovered"]))
	if problems:
		raise AssertionError("endpoint-sweep gate FAILED — " + "; ".join(problems))
	return report
