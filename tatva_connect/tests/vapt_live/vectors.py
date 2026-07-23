# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The 32 VAPT findings as concrete live attack vectors — rebuilt to be SELF-PROVING.

Every payload is built from the endpoint's REAL signature (verified against the live crm/helpdesk/lms
source), and every read/list vector fires TWICE:

    control  — as `admin`, who SHOULD succeed. If admin gets a crash/denial, the PAYLOAD is wrong and
               the vector is BROKEN, never PASS. This is what stops a malformed request masquerading as
               "attacker was blocked".
    attack   — as the low-privilege user, who MUST be refused BY THE PERMISSION LAYER (403 /
               PermissionError). A crash (500 / TypeError) is BROKEN, not a pass — a request that never
               reached the permission engine proves nothing.

Verdicts, unambiguous:
    PASS      attacker denied by perms, and (for reads) admin proved the endpoint really returns data
    FAIL      attacker reached the sensitive data / the write landed  → finding STILL OPEN
    ACCEPTED  public-by-design endpoint, reachable but nothing sensitive (job board, programs, apps, schema)
    BROKEN    the payload crashed the endpoint (my bug) or admin couldn't run it — verdict untrustworthy
    SKIP      no target of that type on the site (nothing to attack)
    REVIEW    a non-permission refusal (validation/404) — a human should look

Output: one line per vector — `[NN] VERDICT  Pn  name  | attacker=… control=… | evidence`.

Personas (from .creds/uat.json): no_role (No App Access), default_user (LMS Student), admin (control
+ seeding + teardown only, never attacks).
"""
import json

from tatva_connect.tests.authz import http_engine

TAG = "vapt-live-vec"
DENY, ACCEPTED = "DENY", "ACCEPTED"
# Exceptions that mean the request CRASHED before the permission engine decided anything — a bad payload,
# never a security verdict. These can never be a PASS.
_CRASH_EXC = {"TypeError", "AttributeError", "KeyError", "NameError", "IndexError", "ValueError"}


# ---- response classification (the one honest judge) ------------------------------------------------
def _classify(code, body):
	"""(kind, evidence). kind ∈ OK | DENIED | CRASH | OTHER — read straight off the wire, never guessed."""
	et = body.get("exc_type") if isinstance(body, dict) else None
	if et in _CRASH_EXC or (code >= 500 and et not in ("PermissionError", "ValidationError")):
		return "CRASH", (et or f"HTTP{code}")
	if code == 403 or et == "PermissionError":
		return "DENIED", (et or "403")
	if 200 <= code < 300 and not (isinstance(body, dict) and body.get("exc_type")):
		return "OK", f"HTTP{code}"
	return "OTHER", (et or f"HTTP{code}")


def _rows(body):
	m = body.get("message") if isinstance(body, dict) else None
	if isinstance(m, list):
		return len(m)
	if isinstance(m, dict):
		for k in ("data", "values"):
			if isinstance(m.get(k), list):
				return len(m[k])
	return 0


def _msg(body):
	return body.get("message") if isinstance(body, dict) else None


# ---- seed / teardown (admin) -----------------------------------------------------------------------
def _admin_insert(eng, doc):
	code, body = eng.call("admin", "frappe.client.insert", http="POST", params={"doc": doc})
	if _classify(code, body)[0] != "OK":
		return None
	m = _msg(body)
	return m.get("name") if isinstance(m, dict) else m


def _find(eng, doctype, filters=None):
	code, body = eng.call("admin", "frappe.client.get_list", http="POST",
	                      params={"doctype": doctype, "filters": filters or {}, "limit_page_length": 1})
	m = _msg(body)
	if _classify(code, body)[0] == "OK" and m:
		return m[0].get("name") if isinstance(m[0], dict) else m[0]
	return None


def _find_all(eng, doctype, filters):
	code, body = eng.call("admin", "frappe.client.get_list", http="POST",
	                      params={"doctype": doctype, "filters": filters, "limit_page_length": 50})
	m = _msg(body)
	return [r.get("name") if isinstance(r, dict) else r for r in m] if _classify(code, body)[0] == "OK" and m else []


def seed(eng):
	ctx = {"found": {}, "seeded": {}}
	for dt in ("Contact", "CRM Lead", "CRM Deal", "CRM Call Log", "HD Ticket", "HD Article", "Job Opportunity"):
		ctx["found"][dt] = _find(eng, dt)
	s, lead = ctx["seeded"], ctx["found"].get("CRM Lead")
	if lead:
		s["Comment"] = _admin_insert(eng, {"doctype": "Comment", "comment_type": "Comment",
		                                   "reference_doctype": "CRM Lead", "reference_name": lead,
		                                   "content": f"{TAG}-comment"})
	s["File"] = _admin_insert(eng, {"doctype": "File", "file_name": f"{TAG}-secret.txt",
	                                "is_private": 1, "content": f"{TAG}-private-bytes"})
	s["LMS Programming Exercise"] = _admin_insert(eng, {
		"doctype": "LMS Programming Exercise", "title": f"{TAG}-ex", "problem_statement": "x",
		"language": "Python", "test_cases": [{"input": f"{TAG}-IN", "expected_output": f"{TAG}-OUT"}]})
	q = _admin_insert(eng, {"doctype": "LMS Question", "question": f"{TAG} pick A", "type": "Choices",
	                        "option_1": "A", "is_correct_1": 1, "option_2": "B", "multiple": 0})
	ctx["question"] = q
	if q:
		s["LMS Quiz"] = _admin_insert(eng, {
			"doctype": "LMS Quiz", "title": f"{TAG}-quiz", "show_answers": 0, "max_attempts": 1,
			"duration": "1", "passing_percentage": 50,
			"questions": [{"question": q, "marks": 1, "type": "Choices"}]})
	return ctx


def teardown(eng, ctx):
	for dt in ("LMS Quiz", "LMS Programming Exercise", "Comment", "File"):
		if ctx["seeded"].get(dt):
			eng.call("admin", "frappe.client.delete", http="POST", params={"doctype": dt, "name": ctx["seeded"][dt]})
	if ctx.get("question"):
		eng.call("admin", "frappe.client.delete", http="POST", params={"doctype": "LMS Question", "name": ctx["question"]})
	if ctx["seeded"].get("LMS Quiz"):
		for sub in _find_all(eng, "LMS Quiz Submission", {"quiz": ctx["seeded"]["LMS Quiz"]}):
			eng.call("admin", "frappe.client.delete", http="POST", params={"doctype": "LMS Quiz Submission", "name": sub})


# ---- the declarative vector table ------------------------------------------------------------------
# Each read/list/create vector is data: how to build the request (from the REAL signature) + what counts
# as a sensitive leak. `control=True` means also fire as admin and require OK, or the payload is BROKEN.
# `params(ctx)` returns the request dict, or None to SKIP when its target is absent.
def _p(**kw):
	return lambda ctx: kw


class V:
	def __init__(self, n, sev, persona, name, expected, method, http, params, sensitive,
	             control=False, target=None, prove_and_stop=False):
		self.n, self.sev, self.persona, self.name, self.expected = n, sev, persona, name, expected
		self.method, self.http, self.params, self.sensitive = method, http, params, sensitive
		self.control, self.target, self.prove_and_stop = control, target, prove_and_stop


def _has_field(body, field):
	m = _msg(body)
	return isinstance(m, dict) and bool(m.get(field))


def _returns_anything(body):
	return bool(_msg(body)) and _rows(body) >= 0 and _msg(body) not in ([], {}, None, "")


def _child_answer_leaked(body):  # N5: test_cases child carries input/expected_output
	tcs = (_msg(body) or {}).get("test_cases") if isinstance(_msg(body), dict) else None
	return bool(tcs) and any((t.get("input") or t.get("expected_output")) for t in tcs)


def _testcase_rows_leaked(body):  # N4: get_list on LMS Test Case returns the answer fields
	rows = _msg(body) or []
	return any(isinstance(r, dict) and (r.get("input") or r.get("expected_output")) for r in rows)


def _owner_leaked(body):  # N-job: creator email
	m = _msg(body) or {}
	return isinstance(m, dict) and bool(m.get("owner"))


def _unpublished_leaked(body):
	rows = _msg(body) or []
	return any(isinstance(r, dict) and r.get("published") in (0, False) for r in rows)


def _tgt(ctx, dt):
	return (ctx["found"].get(dt) or ctx["seeded"].get(dt))


def _table(ctx):
	"""Build the vector list now that ctx (seeded targets) exists — so params bind to real ids."""
	Contact, Lead, Deal, Call = _tgt(ctx, "Contact"), _tgt(ctx, "CRM Lead"), _tgt(ctx, "CRM Deal"), _tgt(ctx, "CRM Call Log")
	Ticket, Article, Job = _tgt(ctx, "HD Ticket"), _tgt(ctx, "HD Article"), _tgt(ctx, "Job Opportunity")
	Comment, Filep, Ex = ctx["seeded"].get("Comment"), ctx["seeded"].get("File"), ctx["seeded"].get("LMS Programming Exercise")

	def read(n, sev, persona, name, method, http, params, sensitive, expected=DENY, target=True):
		return V(n, sev, persona, name, expected, method, http, params, sensitive, control=True, target=target)

	def act(n, sev, persona, name, method, http, params, expected=DENY, target=True):
		# a write/create/delete: the attacker must be denied BY PERMS; success = FAIL (leak).
		return V(n, sev, persona, name, expected, method, http, params, _returns_or_ok, control=False,
		         target=target, prove_and_stop=True)

	return [
		act(1, "P1", "no_role", "Edit another user's Comment (client.set_value)",
		    "frappe.client.set_value", "POST",
		    (_p(doctype="Comment", name=Comment, fieldname="content", value="HACKED") if Comment else None)),
		read(2, "P2", "no_role", "HD Ticket contact info (get_ticket_contact)",
		     "helpdesk.helpdesk.doctype.hd_ticket.api.get_ticket_contact", "POST",
		     (_p(ticket=Ticket) if Ticket else None), _returns_anything),
		read(3, "P2", "no_role", "List all HD Tickets (get_list_data)",
		     "helpdesk.api.doc.get_list_data", "POST", _p(doctype="HD Ticket"), lambda b: _rows(b) > 0),
		read(4, "P2", "no_role", "LMS Job Details — creator email (get_job_details)",
		     "lms.lms.api.get_job_details", "POST", (_p(job=Job) if Job else None), _owner_leaked, expected=ACCEPTED),
		read(5, "P2", "no_role", "Read all Contacts (crm.api.doc.get_data)",
		     "crm.api.doc.get_data", "POST",
		     _p(doctype="Contact", filters={}, order_by="modified desc", page_length=20), lambda b: _rows(b) > 0),
		read(6, "P2", "no_role", "Read any HD Ticket (client.get)",
		     "frappe.client.get", "GET", (_p(doctype="HD Ticket", name=Ticket) if Ticket else None), _returns_anything),
		read(7, "P2", "no_role", "LMS Job Opportunities (get_job_opportunities)",
		     "lms.lms.api.get_job_opportunities", "POST", _p(), lambda b: False, expected=ACCEPTED),
		read(8, "P2", "no_role", "Enumerate LMS Batch drafts (get_batches)",
		     "lms.lms.utils.get_batches", "POST", _p(filters={"published": 0}), _unpublished_leaked),
		read(9, "P2", "no_role", "Assigned users of a CRM Lead (get_assigned_users)",
		     "crm.api.doc.get_assigned_users", "POST",
		     (_p(doctype="CRM Lead", name=Lead) if Lead else None), _returns_anything),
		act(10, "P2", "no_role", "No-App-Access creates HD Ticket (hd_ticket.api.new)",
		    "helpdesk.helpdesk.doctype.hd_ticket.api.new", "POST",
		    _p(doc={"subject": f"{TAG}-intrusion", "description": "x"})),
		V(11, "P2", "default_user", "Quiz answer tampering — server re-grades (submit_quiz)", DENY,
		  None, None, None, None),  # special
		V(12, "P2", "default_user", "Race the single-attempt limit (submit_quiz)", DENY, None, None, None, None),
		read(13, "P2", "default_user", "Peek answers, Show-Answers OFF (check_answer)",
		     "lms.lms.doctype.lms_quiz.lms_quiz.check_answer", "POST",
		     (_p(quiz=ctx["seeded"].get("LMS Quiz"), question=ctx.get("question"), question_type="Choices",
		         answers=json.dumps(["A"])) if ctx["seeded"].get("LMS Quiz") else None),
		     _returns_or_ok, expected=DENY, target=True),
		read(14, "P2", "default_user", "Hidden test cases via get_list (LMS Test Case)",
		     "frappe.client.get_list", "POST",
		     (_p(doctype="LMS Test Case", parent="LMS Programming Exercise",
		         filters={"parent": Ex, "parenttype": "LMS Programming Exercise", "parentfield": "test_cases"},
		         fields=["input", "expected_output", "name"]) if Ex else None), _testcase_rows_leaked),
		read(15, "P2", "default_user", "Exercise answer key via client.get (LMS Programming Exercise)",
		     "frappe.client.get", "GET",
		     (_p(doctype="LMS Programming Exercise", name=Ex) if Ex else None), _child_answer_leaked),
		V(16, "P2", "default_user", "Submit after timer expiry (submit_quiz)", DENY, None, None, None, None),  # special
		read(17, "P3", "no_role", "Read CRM Call Log (get_call_log)",
		     "crm.fcrm.doctype.crm_call_log.crm_call_log.get_call_log", "POST",
		     (_p(name=Call) if Call else None), _returns_anything),
		read(18, "P3", "no_role", "CRM Deal contacts (get_deal_contacts)",
		     "crm.fcrm.doctype.crm_deal.api.get_deal_contacts", "POST",
		     (_p(name=Deal) if Deal else None), _returns_anything),
		act(19, "P3", "no_role", "Delete a Contact (client.delete)",
		    "frappe.client.delete", "POST", (_p(doctype="Contact", name=Contact) if Contact else None)),
		read(20, "P3", "no_role", "Read DocInfo (form.load.getdoc)",
		     "frappe.desk.form.load.getdoc", "GET",
		     (_p(doctype="CRM Lead", name=Lead) if Lead else None), _returns_anything),
		act(21, "P3", "no_role", "Modify a CRM Call Log (client.set_value)",
		    "frappe.client.set_value", "POST",
		    (_p(doctype="CRM Call Log", name=Call, fieldname="status", value="Busy") if Call else None)),
		act(22, "P3", "no_role", "Create a Contact (client.insert)",
		    "frappe.client.insert", "POST", _p(doc={"doctype": "Contact", "first_name": f"{TAG}-intrusion"})),
		read(23, "P3", "no_role", "Linked docs of a CRM Lead (get_linked_docs_of_document)",
		     "crm.api.doc.get_linked_docs_of_document", "POST",
		     (_p(doctype="CRM Lead", docname=Lead) if Lead else None), _returns_anything),
		act(24, "P3", "no_role", "Create a CRM Deal (create_deal)",
		    "crm.fcrm.doctype.crm_deal.crm_deal.create_deal", "POST", _p(doc={"organization": f"{TAG}-org"})),
		read(25, "P3", "no_role", "HD Article stats (get_article_stats)",
		     "helpdesk.api.article.get_article_stats", "POST",
		     (_p(article_name=Article) if Article else None), _returns_anything),
		read(26, "P3", "no_role", "List CRM assignment rules (get_assignment_rules_list)",
		     "crm.api.assignment_rule.get_assignment_rules_list", "POST", _p(), _returns_anything),
		read(27, "P3", "no_role", "HD Ticket activities (get_ticket_activities)",
		     "helpdesk.helpdesk.doctype.hd_ticket.api.get_ticket_activities", "POST",
		     (_p(ticket=Ticket) if Ticket else None), _returns_anything),
		read(28, "P3", "no_role", "Enumerate LMS Course drafts (get_courses)",
		     "lms.lms.utils.get_courses", "POST", _p(filters={"published": 0}), _unpublished_leaked),
		read(29, "P3", "no_role", "LMS Programs (get_programs)",
		     "lms.lms.utils.get_programs", "POST", _p(), lambda b: False, expected=ACCEPTED),
		read(30, "P3", "no_role", "Private file via profile picture (client.get on File)",
		     "frappe.client.get", "GET",
		     (_p(doctype="File", name=Filep) if Filep else None), lambda b: _has_field(b, "file_url")),
		read(31, "P4", "no_role", "List installed apps (get_apps)",
		     "frappe.apps.get_apps", "GET", _p(), lambda b: False, expected=ACCEPTED),
		read(32, "P2", "no_role", "CRM Lead schema dump (getdoctype)",
		     "frappe.desk.form.load.getdoctype", "GET", _p(doctype="CRM Lead"), lambda b: False, expected=ACCEPTED),
	]


def _returns_or_ok(body):
	"""For act()/check_answer: a leak is the endpoint SUCCEEDING (2xx, no exception) as the attacker."""
	return True  # only consulted when the classify() said OK; success itself is the leak


# ---- special multi-step vectors --------------------------------------------------------------------
def _v11_tamper(eng, ctx):
	quiz, q = ctx["seeded"].get("LMS Quiz"), ctx.get("question")
	if not (quiz and q):
		return "SKIP", "no seeded LMS Quiz"
	code, body = eng.call("default_user", "lms.lms.doctype.lms_quiz.lms_quiz.submit_quiz", http="POST",
	                      params={"quiz": quiz, "results": json.dumps([{"question_name": q, "answer": ["B"]}])})
	kind, ev = _classify(code, body)
	if kind == "CRASH":
		return "BROKEN", f"attacker={ev}"
	score = (_msg(body) or {}).get("score") if isinstance(_msg(body), dict) else None
	return ("FAIL" if score else "PASS"), f"wrong-answer score={score} ({ev})"


def _v12_race(eng, ctx):
	quiz, q = ctx["seeded"].get("LMS Quiz"), ctx.get("question")
	if not (quiz and q):
		return "SKIP", "no seeded LMS Quiz"
	who = eng.whoami("default_user")
	for sub in _find_all(eng, "LMS Quiz Submission", {"quiz": quiz, "member": who}):
		eng.call("admin", "frappe.client.delete", http="POST", params={"doctype": "LMS Quiz Submission", "name": sub})
	body_p = {"quiz": quiz, "results": json.dumps([{"question_name": q, "answer": ["A"]}])}
	last = None
	for _ in range(6):
		last = eng.call("default_user", "lms.lms.doctype.lms_quiz.lms_quiz.submit_quiz", http="POST", params=body_p)
	if _classify(*last)[0] == "CRASH":
		return "BROKEN", f"attacker={_classify(*last)[1]}"
	landed = len(_find_all(eng, "LMS Quiz Submission", {"quiz": quiz, "member": who}))
	return ("FAIL" if landed > 1 else "PASS"), f"6 concurrent submits, {landed} landed (max_attempts=1)"


def _v16_timer(eng, ctx):
	quiz = ctx["seeded"].get("LMS Quiz")
	if not quiz:
		return "SKIP", "no seeded LMS Quiz"
	eng.call("default_user", "lms.lms.utils.get_quiz_with_questions", http="POST", params={"quiz": quiz})
	return "SKIP", "timer expiry needs a >duration wait — proven by the bench test test_lms_assessment"


_SPECIAL = {11: _v11_tamper, 12: _v12_race, 16: _v16_timer}


# ---- the runner ------------------------------------------------------------------------------------
def _decide(v, attacker_kind, sensitive, control_kind):
	if attacker_kind == "CRASH":
		return "BROKEN", "attacker crashed the endpoint (payload bug)"
	if v.control and control_kind == "CRASH":
		return "BROKEN", "admin control crashed — payload invalid"
	if v.control and control_kind != "OK":
		return "BROKEN", f"admin control did not succeed ({control_kind}) — cannot prove endpoint"
	if sensitive:
		return "FAIL", "attacker reached the sensitive data / the action landed"
	if v.expected == ACCEPTED and attacker_kind == "OK":
		return "ACCEPTED", "reachable by design, nothing sensitive"
	if attacker_kind == "DENIED":
		return "PASS", "denied by the permission layer"
	if attacker_kind == "OK":
		return "PASS", "reached but returned nothing sensitive"
	return "REVIEW", "non-permission refusal — inspect"


def run(cfg=None):
	from . import config
	cfg = cfg or config.load()
	eng = http_engine.HttpEngine(config.engine_creds(cfg), base=cfg["base"], host=cfg["host"])
	ids = eng.authenticate_all([p for p in ("admin", "no_role", "default_user") if p in cfg["personas"]])
	print("authenticated: " + ", ".join(f"{p}={ids.get(p) or '?'}" for p in ids))

	ctx = seed(eng)
	tally = {k: 0 for k in ("PASS", "FAIL", "ACCEPTED", "BROKEN", "SKIP", "REVIEW")}
	print("\n=== VAPT Jun'26 — 32 live attack vectors (attacker vs admin control, over HTTP) ===")
	try:
		for v in _table(ctx):
			if v.n in _SPECIAL:
				verdict, ev = _SPECIAL[v.n](eng, ctx)
				tally[verdict] = tally.get(verdict, 0) + 1
				print(f"[{v.n:02d}] {verdict:8} {v.sev}  {v.name}  |  {ev}")
				continue
			params = v.params(ctx) if v.params else None
			if params is None:
				tally["SKIP"] += 1
				print(f"[{v.n:02d}] {'SKIP':8} {v.sev}  {v.name}  |  no target on site")
				continue
			# attacker
			ac, ab = eng.call(v.persona, v.method, http=v.http, params=params)
			a_kind, a_ev = _classify(ac, ab)
			sensitive = (a_kind == "OK") and bool(v.sensitive(ab))
			# admin control (reads only)
			c_kind, c_ev = ("-", "-")
			if v.control:
				cc, cb = eng.call("admin", v.method, http=v.http, params=params)
				c_kind, c_ev = _classify(cc, cb)
			# prove-and-stop: undo any write the attacker landed
			if v.prove_and_stop and sensitive:
				nm = (_msg(ab) or {}).get("name") if isinstance(_msg(ab), dict) else (_msg(ab) if isinstance(_msg(ab), str) else None)
				dt = params.get("doctype") or (params.get("doc") or {}).get("doctype")
				if nm and dt:
					eng.call("admin", "frappe.client.delete", http="POST", params={"doctype": dt, "name": nm})
			verdict, why = _decide(v, a_kind, sensitive, c_kind)
			tally[verdict] += 1
			ctl = f" control={c_ev}" if v.control else ""
			print(f"[{v.n:02d}] {verdict:8} {v.sev}  {v.name}  |  attacker={a_ev}{ctl}  |  {why}")
	finally:
		teardown(eng, ctx)

	line = " · ".join(f"{tally[k]} {k}" for k in ("PASS", "FAIL", "ACCEPTED", "BROKEN", "SKIP", "REVIEW") if tally[k])
	print(f"\n=== {line} ===")
	if tally["FAIL"]:
		print(">>> VULNERABILITIES STILL OPEN — see FAIL lines.")
	if tally["BROKEN"]:
		print(">>> BROKEN vectors are NOT passes — their payloads must be fixed before trusting the run.")
	return tally
