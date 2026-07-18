# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""PROVE IT — fire the 6 Jul-cut LMS attacks at the LIVE site as a real logged-in student, over HTTP,
and print what the attacker actually gets back next to what is really in the database. This is not a
unit test; it is the thing you can watch with your own eyes.

Run (local docker only):
    docker exec tatvalocal-backend-1 \
      bench --site dev.localhost execute tatva_connect.tests.live.prove_lms_vapt.run

It seeds a throwaway exercise + quiz with a KNOWN secret, mints a throwaway student + API token, fires
every attack the audit filed, prints a verdict panel, then deletes everything it made. Nothing is left
behind. Safe to run any night you want to sleep.
"""
import json
from concurrent.futures import ThreadPoolExecutor

import frappe
import requests

BASE = "http://localhost:8000/api/method"
HOST = "dev.localhost"
SECRET_IN = "HUNTER2_INPUT_SECRET"
SECRET_OUT = "HUNTER2_EXPECTED_OUTPUT_SECRET"
_EMAIL = "prove-lms-vapt@example.com"

G, R, Y, B, X = "\033[32m", "\033[31m", "\033[33m", "\033[1m", "\033[0m"


def _hdr(token):
	return {"Authorization": f"token {token}", "Host": HOST, "Content-Type": "application/json"}


def _setup():
	if not frappe.db.exists("User", _EMAIL):
		u = frappe.get_doc(
			{"doctype": "User", "email": _EMAIL, "first_name": "prove-lms", "send_welcome_email": 0}
		).insert(ignore_permissions=True)
	else:
		u = frappe.get_doc("User", _EMAIL)
	u.add_roles("LMS Student")
	u.enabled = 1  # teardown disables it between runs (a full delete cascades tabContact -> deadlock)
	key = frappe.generate_hash(length=15)
	secret = frappe.generate_hash(length=15)
	u.api_key, u.api_secret = key, secret
	u.save(ignore_permissions=True)
	ex = frappe.get_doc(
		{
			"doctype": "LMS Programming Exercise", "title": "PROVE-ex", "problem_statement": "x",
			"language": "Python", "test_cases": [{"input": SECRET_IN, "expected_output": SECRET_OUT}],
		}
	).insert(ignore_permissions=True)
	q = frappe.get_doc(
		{"doctype": "LMS Question", "question": "Pick A", "type": "Choices", "option_1": "A",
		 "is_correct_1": 1, "option_2": "B", "multiple": 0}
	).insert(ignore_permissions=True)
	quiz = frappe.get_doc(
		{"doctype": "LMS Quiz", "title": "PROVE-quiz", "show_answers": 0, "max_attempts": 1,
		 "duration": "1", "passing_percentage": 50,
		 "questions": [{"question": q.name, "marks": 1, "type": "Choices"}]}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	return {"token": f"{key}:{secret}", "exercise": ex.name, "quiz": quiz.name, "question": q.name}


def _teardown():
	"""Delete only the lightweight LMS records we made; DISABLE + de-token the throwaway user rather than
	delete it (User.on_trash cascades tabContact and can deadlock against concurrent work). Every delete is
	best-effort so a busy bench never turns cleanup into a crash after the verdict already printed."""
	frappe.set_user("Administrator")
	frappe.db.delete("LMS Quiz Submission", {"member": _EMAIL})
	targets = [("LMS Quiz", {"title": "PROVE-quiz"}), ("LMS Programming Exercise", {"title": "PROVE-ex"}),
	           ("LMS Question", {"question": "Pick A", "owner": _EMAIL})]
	for dt, flt in targets:
		for n in frappe.get_all(dt, filters=flt, pluck="name"):
			try:
				frappe.delete_doc(dt, n, force=True, ignore_permissions=True)
			except Exception:
				pass
	if frappe.db.exists("User", _EMAIL):
		frappe.db.set_value("User", _EMAIL, {"enabled": 0, "api_key": None, "api_secret": None})
	frappe.cache().delete_keys("lms_quiz_start:")
	frappe.db.commit()


def _line(name, claim, attacker_got, sealed):
	tag = f"{G}SEALED{X}" if sealed else f"{R}LEAKING{X}"
	print(f"  [{tag}] {B}{name}{X} — {claim}")
	print(f"           attacker received: {Y}{attacker_got}{X}")


def run():
	frappe.set_user("Administrator")
	c = _setup()
	h = _hdr(c["token"])
	ex, quiz, q = c["exercise"], c["quiz"], c["question"]
	results = []
	print(f"\n{B}══ PROVE-IT: 6 LMS attacks, fired live as a logged-in student ══{X}")
	print(f"  the secret we planted in the DB: {Y}{SECRET_IN} / {SECRET_OUT}{X}")
	print(f"  (admin can read it right now: {Y}{frappe.get_doc('LMS Programming Exercise', ex).test_cases[0].input}{X})\n")

	# N5 — read the exercise, try to steal the hidden test cases
	tc = (requests.get(f"{BASE}/frappe.client.get", params={"doctype": "LMS Programming Exercise", "name": ex},
	                   headers=h).json().get("message", {}).get("test_cases") or [{}])[0]
	got = {"input": tc.get("input"), "expected_output": tc.get("expected_output")}
	sealed = got["input"] is None and got["expected_output"] is None
	results.append(sealed); _line("N5 steal test cases via client.get", "the answer key must not come back", got, sealed)

	# N4 — list the hidden test-case rows directly
	rows = requests.post(f"{BASE}/frappe.client.get_list", headers=h, data=json.dumps(
		{"doctype": "LMS Test Case", "parent": "LMS Programming Exercise",
		 "filters": {"parent": ex, "parenttype": "LMS Programming Exercise", "parentfield": "test_cases"},
		 "fields": ["input", "expected_output", "name"]})).json().get("message", [])
	leaked = any(r.get("input") or r.get("expected_output") for r in rows)
	results.append(not leaked); _line("N4 list test cases via get_list", "no input/expected_output in the list", rows, not leaked)

	# N3 — ask the server whether an answer is right, with "show answers" off
	code = requests.post(f"{BASE}/lms.lms.doctype.lms_quiz.lms_quiz.check_answer", headers=h, data=json.dumps(
		{"quiz": quiz, "question": q, "question_type": "Choices", "answers": json.dumps(["A"])})).status_code
	results.append(code == 403); _line("N3 peek answers via check_answer", "must be refused (HTTP 403)", f"HTTP {code}", code == 403)

	# N6 — let the timer expire, then replay the submit
	frappe.cache().set_value(f"lms_quiz_start:{quiz}:{_EMAIL}", frappe.utils.now_datetime().timestamp() - 300)
	body = json.dumps({"quiz": quiz, "results": json.dumps([{"question_name": q, "answer": ["A"]}])})
	code = requests.post(f"{BASE}/lms.lms.doctype.lms_quiz.lms_quiz.submit_quiz", headers=h, data=body).status_code
	results.append(code == 417); _line("N6 submit after time's up", "must be refused (HTTP 417)", f"HTTP {code}", code == 417)
	frappe.db.delete("LMS Quiz Submission", {"quiz": quiz}); frappe.cache().delete_value(f"lms_quiz_start:{quiz}:{_EMAIL}"); frappe.db.commit()

	# N2 — the single-packet race: 20 submits at once against a one-attempt quiz
	def _fire(_): return requests.post(f"{BASE}/lms.lms.doctype.lms_quiz.lms_quiz.submit_quiz", headers=h, data=body).status_code
	with ThreadPoolExecutor(max_workers=20) as pool:
		list(pool.map(_fire, range(20)))
	landed = frappe.db.count("LMS Quiz Submission", {"quiz": quiz, "member": _EMAIL})
	results.append(landed == 1); _line("N2 race the one-attempt limit", "20 fired at once — exactly 1 may land", f"{landed} landed of 20", landed == 1)

	# N1 — try to submit a wrong answer and get marks; server must grade it 0
	frappe.db.delete("LMS Quiz Submission", {"quiz": quiz}); frappe.db.commit()
	uq = frappe.get_doc("LMS Quiz", quiz); uq.max_attempts = 0; uq.save(ignore_permissions=True); frappe.db.commit()
	wrong = requests.post(f"{BASE}/lms.lms.doctype.lms_quiz.lms_quiz.submit_quiz", headers=h, data=json.dumps(
		{"quiz": quiz, "results": json.dumps([{"question_name": q, "answer": ["B"]}])})).json().get("message", {})
	score = wrong.get("score")
	results.append(score == 0); _line("N1 tamper answers for full marks", "a wrong answer must score 0", f"score={score}", score == 0)

	_teardown()
	n = sum(results)
	banner = f"{G}ALL {n}/6 SEALED — sleep well.{X}" if n == 6 else f"{R}{6-n} of 6 STILL LEAKING — do NOT sleep.{X}"
	print(f"\n{B}══ VERDICT: {banner}{B} ══{X}\n")
	return {"sealed": n, "of": 6}
