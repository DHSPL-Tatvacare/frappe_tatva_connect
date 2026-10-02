# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""No HTTP endpoint lets a hostile persona do more to a foreign-owned row than native Frappe allows.
HTTP runs on another connection, so this seeds with commit=True and tears down by tag."""
import os
import time

import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.tests.authz import generator, http_engine, oracle, roster
from tatva_connect.tests.authz.comms import assert_comms_off
from tatva_connect.tests.authz.registry import cases, endpoints
from tatva_connect.tests.authz.vapt import findings

TAG = generator.TAG

# One foreign-owned row per doctype, so a case tests object-level access, not a 404 on a missing id.
_FOREIGN_SEED = {
	"Contact": {"first_name": f"{TAG}-contact"},
	# reference_name is set at seed time to the foreign lead, since a Comment rejects a null parent.
	"Comment": {"comment_type": "Comment", "reference_doctype": "CRM Lead", "content": f"{TAG}-comment"},
	"ToDo": {"description": f"{TAG}-todo", "allocated_to": "Administrator"},
	# Seeded first so CRM Deal can link a real org.
	"CRM Organization": {"organization_name": f"{TAG}-org"},
	"CRM Deal": {},  # special-cased in _seed_foreign_objects (org Link + reqd status resolved live)
	"HD Ticket": {"subject": f"{TAG}-ticket"},
	"CRM Call Log": {"id": f"{TAG}-call", "from": "+910000000000", "to": "+910000000001",
	                 "type": "Incoming", "status": "Completed"},
	"Assignment Rule": {"name": f"{TAG}-arule", "document_type": "ToDo", "assign_condition": "1",
	                    "rule": "Round Robin", "users": [{"user": "Administrator"}]},
	# A private File owned by Administrator, so a hostile read is a real private-file IDOR test.
	"File": {"file_name": f"{TAG}-secret.txt", "is_private": 1, "content": f"{TAG}-file-body"},
	"FCRM Note": {"title": f"{TAG}-note", "content": f"{TAG}-note-body"},
	# An exercise with a hidden answer key, so the LMS cases have a foreign-owned target.
	"LMS Programming Exercise": {"title": f"{TAG}-exercise", "problem_statement": "x", "language": "Python",
	                             "test_cases": [{"input": f"{TAG}-in", "expected_output": f"{TAG}-out"}]},
	# A null wiki_space makes an orphan readable by all, the weakest case, so a denial here holds everywhere.
	"Wiki Document": {"title": f"{TAG}-wikidoc"},
	# Seeded before Insights Query v3, whose only required field links it.
	"Insights Workbook": {"title": f"{TAG}-workbook"},
	"Insights Query v3": {},  # special-cased in _seed_foreign_objects (workbook Link resolved live)
}
_ENDPOINT_BY_KEY = {e.key: e for e in (*endpoints.GENERIC_ENDPOINTS, *endpoints.APP_ENDPOINTS)}


def _seed_foreign_objects():
	"""Return {doctype: name} of a foreign-owned instance per sensitive doctype. Best-effort + logged."""
	owned = {}
	lead = frappe.db.get_value("CRM Lead", {"first_name": ["like", f"%{TAG}%"]}, "name")
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
		if doctype == "Insights Query v3":  # resolve the workbook Link (seeded above), live
			payload = {"title": f"{TAG}-query", "workbook": owned.get("Insights Workbook")}
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
	"""Delete every row _seed_foreign_objects planted, which generator.teardown() does not know about.
	The tag field comes from the seed payload, so a new _FOREIGN_SEED doctype is torn down too."""
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
	"""Clean up the committed run, retrying with backoff while workers settle.
	Each try rolls back first, so a stale read snapshot cannot raise MariaDB 1020 on the delete."""
	for attempt in range(1, 6):
		frappe.db.rollback()  # drop the stale snapshot that causes the 1020 error
		try:
			_teardown_foreign_objects()
			generator.teardown()
			return
		except Exception:  # authz-ok: cleanup-only retry; concurrent-commit vs teardown race (1020, class varies)
			if attempt == 5:
				raise
			time.sleep(attempt * 2)


def _list_row_count(msg):
	"""Rows a list or report endpoint returned, ignoring the columns/keys metadata around them.
	Handles {data:[...]}, {values:[...]} and a plain list."""
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
	"""True when the endpoint did the thing and native would deny it.
	The sweep and mutation._endpoint_escalation share this one predicate."""
	return _endpoint_allowed(action, code, body) and not native_ok


def run(base=None, host="dev.localhost"):
	# The sweep runs inside the bench, so it hits the internal gunicorn url unless AUTHZ_HTTP_BASE overrides it.
	base = base or os.environ.get("AUTHZ_HTTP_BASE") or "http://localhost:8000"
	assert_comms_off()
	generator.seed(commit=True)
	foreign = _seed_foreign_objects()
	frappe.db.commit()
	eng = http_engine.HttpEngine(http_engine.load_creds(generator.creds_path()), base, host)
	generated = cases.generate_http_cases()

	escalations, skipped, results = [], [], []
	for c in generated:
		spec = _ENDPOINT_BY_KEY[c.endpoint_key]
		obj_id = foreign.get(c.doctype) if c.doctype else None
		if c.action in ("read", "write", "delete") and c.doctype and not obj_id:
			skipped.append(c.id)  # keep the id: a count alone cannot say WHICH endpoint proved nothing
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
	# Print the verdict before teardown, so a cleanup failure never loses the escalation list.
	print(f"[endpoint-sweep] cases={len(generated)} skipped={len(skipped)} "
	      f"escalations={len(real)} benign_residuals={len(benign)} vapt_uncovered={len(uncovered)}")
	for e in real:
		print(f"  ESCALATION {e['case']} (HTTP {e['code']}) {e['method']}")
	for e in benign:
		print(f"  benign-residual {e['case']} ({e['endpoint_key']}/{e['doctype']}) — allowlisted")
	# A skipped case proved nothing, so name each one.
	for doctype in sorted({i.rsplit("-", 1)[-1] for i in skipped}):
		ids = [i for i in skipped if i.endswith(doctype)]
		print(f"  skip-no-target {doctype}: {len(ids)} case(s) — no foreign-owned row was seeded")
	if uncovered:
		print(f"  VAPT COVERAGE GAP: {uncovered}")
	assert_residuals_benign(eng)  # re-prove the allowlist is still non-sensitive (trap check)
	_teardown_all()
	return report


def assert_residuals_benign(eng):
	"""A foreign private File stays out of every list endpoint and unreadable to a plain persona.
	Fails the run if findings.BENIGN_RESIDUALS ever stops being harmless."""
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
	"""CI entry: run the sweep and raise on any escalation or VAPT coverage gap.
	Separate from run(), which only produces the report."""
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


class TestEndpointSweepIsNotSilent(IntegrationTestCase):
	"""Gives `run-tests --module` real checks here, so it never passes with zero tests collected.
	The sweep itself needs committed HTTP personas and runs from `gate`."""

	def test_every_generated_case_maps_to_a_known_endpoint(self):
		"""Every generated case names a registered endpoint, so a typo fails here, not mid-sweep."""
		unknown = sorted({c.endpoint_key for c in cases.generate_http_cases()} - set(_ENDPOINT_BY_KEY))
		self.assertFalse(unknown, f"generated cases reference unknown endpoint keys: {unknown}")

	def test_every_seedable_doctype_is_a_sensitive_doctype(self):
		"""A _FOREIGN_SEED entry for a doctype nobody sweeps plants a row every run and proves nothing."""
		sensitive = {d["doctype"] for d in endpoints.SENSITIVE_DOCTYPES}
		prerequisites = {"CRM Organization", "Insights Workbook"}  # seeded only so a Link below resolves
		stray = sorted(set(_FOREIGN_SEED) - sensitive - prerequisites)
		self.assertFalse(stray, f"seeded but never swept: {stray}")
