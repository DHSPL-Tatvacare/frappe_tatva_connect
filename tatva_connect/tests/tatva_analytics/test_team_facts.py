"""Team facts read as today's boards do, and reach exactly the people the records behind them reach.

A pool's assignments carry fair share, skip reasons, first and moved-off as the live Assignments board counts them; a
person's shift week reads as Team Day does; the Sales Hierarchy and a business-line User Permission narrow who reads them.

Run:
    bench --site dev.localhost run-tests --app tatva_connect --module tatva_connect.tests.tatva_analytics.test_team_facts
"""
from collections import defaultdict
from datetime import datetime, time, timedelta

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, getdate, now_datetime

from tatva_connect.lead import assignment
from tatva_connect.tatva_analytics import engine
from tatva_connect.tatva_analytics.models import lead_allocation_fact as allocation
from tatva_connect.tatva_analytics.models import pool_backlog_summary as backlog
from tatva_connect.tatva_analytics.models import shift_activity_fact as shift

POOL = "ZZ Team Facts Pool"
A, B, C = "zz.facts.a@example.com", "zz.facts.b@example.com", "zz.facts.c@example.com"
GRAIN = {"grain_vertical": "Goodflip", "grain_group": "India", "grain_program": "Inside-Sales"}


def refresh(model, started=None, watermark=None):
	"""One run of a job, the way engine.run calls it: a first load with no watermark, a delta with one."""
	return model.refresh(frappe._dict(started=started or now_datetime(), watermark=watermark, window_days=model.WINDOW_DAYS,
	                                  write=engine.write, checkpoint=lambda: None))


def user(email, *roles):
	if not frappe.db.exists("User", email):
		frappe.get_doc({"doctype": "User", "email": email, "first_name": email.split("@")[0], "send_welcome_email": 0}).insert(ignore_permissions=True)
	if roles:
		frappe.get_doc("User", email).add_roles(*roles)
	return email


def step(winner, **skipped):
	"""A draw's step row text, written by the assignment code's own writer, so a wording change turns this red."""
	return f"{assignment.ASSIGNED}{winner}" + assignment.skipped_note({f"zz.facts.{k}@example.com": why for k, why in skipped.items()})


def lead(name):
	return frappe.get_doc({"doctype": "CRM Lead", "first_name": name, "lead_name": name, "status": "New"}).insert(ignore_permissions=True).name


class TeamFactCase(IntegrationTestCase):
	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def pool(self, at, members, require_checkin=0, work_shift=None, disabled=1):
		with self.freeze_time(at):
			return frappe.get_doc({"doctype": "Assignment Rule", "name": POOL, "document_type": "CRM Lead", "description": "facts",
			                       "rule": "Credit Weighted", "disabled": disabled, "require_checkin": require_checkin, **GRAIN,
			                       "assignment_days": [{"day": "Monday"}],
			                       "weighted_users": [{"user": u, "weight": w, "work_shift": work_shift} for u, w in members]}
			                      ).insert(ignore_permissions=True)

	def assign(self, at, lead_name, winner, detail=None, cancel=False):
		with self.freeze_time(at):
			todo = frappe.get_doc({"doctype": "ToDo", "allocated_to": winner, "reference_type": "CRM Lead", "reference_name": lead_name,
			                       "assignment_rule": POOL, "description": "facts"}).insert(ignore_permissions=True)
		if detail:
			with self.freeze_time(at + timedelta(seconds=2)):
				frappe.get_doc({"doctype": "CRM Workflow Step Log", "journey": "zz-facts", "subject_name": lead_name,
				                "node_type": "Distribute", "detail": detail}).insert(ignore_permissions=True, ignore_links=True)
		if cancel:
			todo.status = "Cancelled"
			todo.save(ignore_permissions=True)


class TestLeadAllocationFact(TeamFactCase):
	def test_an_assignments_numbers_read_as_the_live_board_counts_them(self):
		for email in (A, B, C):
			user(email, "Sales User")
		day = datetime.combine(getdate() - timedelta(days=1), time(10))
		self.pool(day - timedelta(hours=1), [(A, 2), (B, 1), (C, 1)])
		l0, l1, l2 = lead("ZZ Facts Zero"), lead("ZZ Facts One"), lead("ZZ Facts Two")
		self.assign(day, l0, A, step(A, b="away", c="not_checked_in"), cancel=True)
		self.assign(day + timedelta(minutes=30), l1, B, step(B, a="untouched_limit"))
		self.assign(day + timedelta(minutes=90), l2, A)  # no step row: it counts for its winner alone
		self.assign(day + timedelta(hours=3), l0, C, step(C, a="cap", b="paused"))  # l0 handed on

		refresh(allocation)

		rows = frappe.get_all(allocation.TARGET, filters={"pool": POOL}, fields=["todo", "user", "share", "won", "first_draw", "moved_off", "in_share", "why"])
		per = defaultdict(lambda: [0.0, 0, 0, 0, 0])
		shares = defaultdict(float)
		for r in rows:
			per[r.user][0] += flt(r.share)
			per[r.user][1] += r.won
			per[r.user][2] += r.won and r.first_draw
			per[r.user][3] += r.won and r.moved_off
			per[r.user][4] += r.in_share
			shares[r.todo] += flt(r.share)
		self.assertEqual({u: (round(v[0], 3), *v[1:]) for u, v in per.items()},
		                 {A: (2.833, 2, 2, 1, 4), B: (0.583, 1, 1, 0, 2), C: (0.583, 1, 0, 0, 2)})
		self.assertTrue(all(abs(total - 1) < 1e-9 for total in shares.values()), shares)
		skipped = sorted((r.user, r.why) for r in rows if r.why not in ("", "won"))
		self.assertEqual(skipped, sorted([(A, "untouched_limit"), (A, "cap"), (B, "away"), (B, "paused"), (C, "not_checked_in")]))

	def test_a_later_run_carries_a_cancellation_and_a_delete_and_keeps_every_id(self):
		for email in (A, B):
			user(email, "Sales User")
		day = datetime.combine(getdate() - timedelta(days=1), time(10))
		self.pool(day - timedelta(hours=1), [(A, 1), (B, 1)])
		l0, l1 = lead("ZZ Facts Kept"), lead("ZZ Facts Gone")
		self.assign(day, l0, A, step(A, b="away"))
		self.assign(day + timedelta(minutes=30), l1, B, step(B, a="away"))
		first = now_datetime()
		refresh(allocation, first)
		ids = {r.grain_key: r.name for r in frappe.get_all(allocation.TARGET, filters={"pool": POOL}, fields=["name", "grain_key"])}

		todo = frappe.get_doc("ToDo", {"reference_name": l0, "assignment_rule": POOL})
		todo.status = "Cancelled"
		todo.save(ignore_permissions=True)
		gone = frappe.db.get_value("ToDo", {"reference_name": l1, "assignment_rule": POOL})
		frappe.delete_doc("ToDo", gone, force=True, ignore_permissions=True)
		refresh(allocation, now_datetime() + timedelta(seconds=1), watermark=first)

		rows = frappe.get_all(allocation.TARGET, filters={"pool": POOL}, fields=["name", "grain_key", "todo", "won", "moved_off"])
		self.assertEqual({r.grain_key: r.name for r in rows}, {k: v for k, v in ids.items() if k in {r.grain_key for r in rows}},
		                 "every row kept its own id")
		self.assertFalse([r for r in rows if r.todo == gone], "a deleted ToDo takes its rows with it")
		self.assertEqual([(r.won, r.moved_off) for r in rows if r.todo == todo.name and r.won], [(1, 1)], "a cancelled ToDo moves the lead off")
		self.assertEqual(len(rows), len({r.grain_key for r in rows}), "no grain written twice")

	def test_the_sales_hierarchy_and_a_business_line_narrow_who_reads_the_rows(self):
		from crm.permissions.test_org_hierarchy import make_hierarchy_node

		manager, rep, loner, outsider = (user("zz.facts.mgr@example.com", "Sales Manager"), user(A, "Sales User"),
		                                 user(B, "Sales User"), user("zz.facts.out@example.com", "Sales Manager"))
		fcrm = frappe.get_single("FCRM Settings")
		fcrm.enable_sales_hierarchy = 1
		fcrm.save(ignore_permissions=True)
		boss = make_hierarchy_node(manager, is_group=1)
		make_hierarchy_node(rep, reports_to=boss.name)
		other = {**GRAIN, "grain_group": "ZZ Facts Other Group"}
		engine.write(allocation.TARGET, [
			{"name": f"zz-facts-{who}-{i}", "user": who, "day": getdate(), "won": 1, **grain}
			for i, (who, grain) in enumerate([(rep, GRAIN), (loner, GRAIN), (loner, other)])])

		def seen(as_user):
			frappe.set_user(as_user)
			names = sorted(frappe.get_list(allocation.TARGET, filters={"name": ("like", "zz-facts-%")}, pluck="name"))
			frappe.set_user("Administrator")
			return names

		self.assertEqual(seen(rep), ["zz-facts-zz.facts.a@example.com-0"], "a rep in the tree reads their own rows")
		self.assertEqual(seen(loner), ["zz-facts-zz.facts.b@example.com-1", "zz-facts-zz.facts.b@example.com-2"], "a rep outside it reads their own")
		self.assertEqual(seen(manager), ["zz-facts-zz.facts.a@example.com-0"], "a manager in the tree reads their subtree")
		self.assertEqual(len(seen(outsider)), 3, "a manager outside the tree reads everyone")
		frappe.get_doc({"doctype": "User Permission", "user": outsider, "allow": "CRM Group", "for_value": "India",
		                "apply_to_all_doctypes": 1}).insert(ignore_permissions=True)
		self.assertEqual(len(seen(outsider)), 2, "a Group User Permission keeps that Group's rows only")


class TestShiftActivityFact(TeamFactCase):
	def test_a_week_reads_as_team_day_counts_it(self):
		user(A, "Sales User")
		today = getdate()
		wed = today - timedelta(days=(today.weekday() - 2) % 7 or 7)
		sun, mon, tue = wed - timedelta(days=3), wed - timedelta(days=2), wed - timedelta(days=1)
		at = lambda day, hhmm: datetime.combine(day, time(*map(int, hhmm.split(":"))))  # noqa: E731
		with self.freeze_time(at(sun, "08:00")):
			work = frappe.get_doc({"doctype": "Tatva Work Shift", "shift_name": "ZZ Facts Shift", **GRAIN,
			                       "working_hours": [{"workday": d, "start_time": "10:00:00", "end_time": "19:00:00"}
			                                         for d in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")]}
			                      ).insert(ignore_permissions=True)
		lead_name = lead("ZZ Facts Week")  # before the pool is on, so the pool never draws it
		# The shift fact reads members of ENABLED check-in pools only.
		self.pool(at(sun, "08:00"), [(A, 1)], require_checkin=1, work_shift=work.name, disabled=0)

		def checkin(when, status, **extra):
			with self.freeze_time(when):
				row = frappe.get_doc({"doctype": "Tatva Activity Log", "user": A, "event": "Check-in", "status": status, **extra})
				row.flags.checkin_source = "Self"
				row.insert(ignore_permissions=True)

		checkin(at(sun, "11:00"), "Active")
		checkin(at(sun, "15:00"), "Unavailable")
		checkin(at(mon, "18:00"), "Active")  # never checked out on Monday: Tuesday carries it
		checkin(at(tue, "19:00"), "Unavailable")
		checkin(at(wed, "09:50"), "Active")
		with self.freeze_time(at(wed, "11:40")):
			frappe.get_doc({"doctype": "Tatva Activity Log", "user": A, "event": "Away", "status": "Away",
			                "last_active_at": at(wed, "11:00")}).insert(ignore_permissions=True)
		checkin(at(wed, "13:00"), "Away")
		checkin(at(wed, "13:30"), "Active")
		checkin(at(wed, "19:00"), "Unavailable")
		self.assign(at(wed, "10:30"), lead_name, A, step(A))
		self.assign(at(wed, "20:00"), lead_name, A, step(A))  # after the shift: not counted
		for when, secs in ((at(wed, "12:00"), 300), (at(wed, "21:00"), 60)):
			with self.freeze_time(when):
				frappe.get_doc({"doctype": "CRM Call Log", "id": frappe.generate_hash(length=12), "type": "Outgoing", "status": "Completed",
				                "caller": A, "from": "1", "to": "2", "duration": secs, "start_time": when,
				                "end_time": when + timedelta(seconds=secs)}).insert(ignore_permissions=True)

		refresh(allocation)
		refresh(shift)
		ids = {r.grain_key: r.name for r in frappe.get_all(shift.TARGET, filters={"user": A}, fields=["name", "grain_key"])}
		refresh(shift, now_datetime() + timedelta(seconds=1), watermark=now_datetime() - timedelta(hours=1))
		again = {r.grain_key: r.name for r in frappe.get_all(shift.TARGET, filters={"user": A}, fields=["name", "grain_key"])}
		self.assertEqual(again, ids, "a re-run writes the same grains, each keeping its own id")

		rows = frappe.get_all(shift.TARGET, filters={"user": A}, fields=["*"])
		day = defaultdict(lambda: defaultdict(float))
		for r in rows:
			d = day[r.day]
			for k in ("working_h", "idle_h", "idle_gaps", "break_h", "breaks", "leads", "calls", "connected", "talk_min", "minutes_late"):
				d[k] += flt(r[k])
			d["idle_min"] = max(d["idle_min"], flt(r.idle_min))
			d["first"] = r.first_check_in or d.get("first")
			d["out"] = r.checked_out or d.get("out")
		self.assertNotIn(sun, day, "Sunday is not a shift day")
		self.assertFalse([r for r in rows if r.day < sun], "nothing before they joined the pool")
		self.assertFalse([r for r in rows if r.kind == "Not checked in" and r.day in (mon, tue, wed)], "a day with an Active check-in, or one carried over, is not absent")
		self.assertEqual((round(day[mon]["working_h"], 2), day[mon]["first"], day[mon]["minutes_late"]), (1.0, "18:00", 480))
		self.assertEqual((round(day[tue]["working_h"], 2), day[tue].get("first"), day[tue]["out"]), (9.0, None, "19:00"))
		w = day[wed]
		self.assertEqual((round(w["working_h"], 2), round(w["idle_h"], 2), w["idle_gaps"], w["idle_min"], w["break_h"], w["breaks"],
		                  w["first"], w["minutes_late"], w["out"], w["leads"], w["calls"], w["connected"], w["talk_min"]),
		                 (7.83, 0.67, 1, 40.0, 0.5, 1, "09:50", 0, "19:00", 1, 1, 1, 5.0))


class TestPoolBacklogSummary(TeamFactCase):
	def test_each_hour_keeps_its_snapshot_and_only_the_newest_is_latest(self):
		self.pool(now_datetime() - timedelta(days=1), [])
		ten = datetime.combine(getdate(), time(10, 5))
		refresh(backlog, ten)
		refresh(backlog, ten + timedelta(minutes=30))  # the same hour updates its row
		refresh(backlog, ten + timedelta(hours=1))
		rows = frappe.get_all(backlog.TARGET, filters={"pool": POOL}, fields=["snapshot", "is_latest"], order_by="snapshot")
		self.assertEqual([(str(r.snapshot), r.is_latest) for r in rows],
		                 [(str(ten.replace(minute=0)), 0), (str(ten.replace(minute=0) + timedelta(hours=1)), 1)])
