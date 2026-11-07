"""One row per stretch or moment of a person's shift day: working, idle, break, each status change, each lead received, each call, and a day with no check-in."""

import frappe
from frappe.utils import getdate

from tatva_connect.lead.assignment_rule import WEIGHTED
from tatva_connect.tatva_analytics import facts
from tatva_connect.tatva_analytics.models import lead_allocation_fact

JOB = "shift_activity_fact"
TARGET = "Tatva Shift Activity Fact"
AFTER = lead_allocation_fact.JOB
WINDOW_DAYS = 30
USER_CHUNK = 100
PROBE = {"users": ("",), "lo": "1900-01-01", "hi": "1900-01-01", "now": "1900-01-01"}
LOG, CALL, TODO, MEMBER = "Tatva Activity Log", "CRM Call Log", "ToDo", "Assignment Rule User"


def quoted(values):
	return ", ".join(frappe.db.escape(v) for v in values)


def people_sql():
	"""Each member of an enabled pool that requires check-in and names a Work Shift, once per business line, with that shift."""
	weighted = quoted(WEIGHTED)
	return f"""SELECT u.user, r.grain_vertical, r.grain_group, r.grain_program,
         CONCAT_WS('|', COALESCE(r.grain_vertical, ''), COALESCE(r.grain_group, ''), COALESCE(r.grain_program, '')) AS line,
         MIN(u.work_shift) AS shift, MIN(u.paused) AS paused, MIN(u.creation) AS joined, MIN(u.name) AS member
  FROM `tabAssignment Rule User` u
  JOIN `tabAssignment Rule` r ON r.name = u.parent AND r.document_type = 'CRM Lead' AND r.disabled = 0 AND r.require_checkin = 1
  WHERE u.parenttype = 'Assignment Rule' AND u.parentfield = IF(r.rule IN ({weighted}), 'weighted_users', 'users')
    AND COALESCE(u.work_shift, '') <> ''
  GROUP BY u.user, r.grain_vertical, r.grain_group, r.grain_program"""


def select_sql(days=WINDOW_DAYS):
	"""Every stretch and moment of %(users)s on their shift days in %(lo)s..%(hi)s (at most `days` + 1 days), clipped to their shift and %(now)s.

	Stretches: Working and Idle cut from each Active spell (so idle never exceeds checked-in time), Break from each Away
	spell; the status at a day's start carries over from the newest row before it. Idle is the system's Away rows, an
	overlap reported twice counted once. Moments: each status change, each lead the person won, each outgoing call.
	Each row names the record it came from; a day with no check-in names the pool membership that made the person expected."""
	offsets = " UNION ALL ".join(f"SELECT {n} AS n" for n in range(days + 1))
	key = facts.grain_key("ev.user", "sd.grain_vertical", "sd.grain_group", "sd.grain_program", "ev.src_doctype", "ev.src_name", "ev.kind", "ev.s")
	return f"""WITH people AS ({people_sql()}),
cohort AS (SELECT * FROM people WHERE user IN %(users)s),
days AS (SELECT CAST(%(lo)s AS DATE) + INTERVAL n DAY AS d FROM ({offsets}) x WHERE CAST(%(lo)s AS DATE) + INTERVAL n DAY <= CAST(%(hi)s AS DATE)),
-- A person's shift day: one of their Work Shift's days since they joined the pool, not named by a holiday list covering its line.
shift_days AS (
  SELECT c.user, c.line, c.grain_vertical, c.grain_group, c.grain_program, c.paused, c.member, dy.d,
         MIN(TIMESTAMP(dy.d, sd.start_time)) AS ss, MAX(TIMESTAMP(dy.d, sd.end_time)) AS se
  FROM cohort c JOIN days dy ON dy.d >= DATE(c.joined)
  JOIN `tabCRM Service Day` sd ON sd.parenttype = 'Tatva Work Shift' AND sd.parentfield = 'working_hours'
       AND sd.parent = c.shift AND sd.workday = DAYNAME(dy.d)
  WHERE NOT EXISTS (
    SELECT 1 FROM `tabCRM Holiday` h JOIN `tabCRM Holiday List` hl ON hl.name = h.parent
    WHERE h.parenttype = 'CRM Holiday List' AND h.date = dy.d
      AND COALESCE(hl.grain_vertical, '') IN ('', COALESCE(c.grain_vertical, ''))
      AND COALESCE(hl.grain_group, '') IN ('', COALESCE(c.grain_group, ''))
      AND COALESCE(hl.grain_program, '') IN ('', COALESCE(c.grain_program, '')))
  GROUP BY c.user, c.line, dy.d
),
-- The batch's outgoing calls over the span, read once by the creation index (a call log has no caller index); DISTINCT keeps it one read.
calls AS (
  SELECT DISTINCT cl.name, cl.caller, cl.creation, cl.start_time, cl.end_time, cl.status, cl.duration, cl.reference_docname
  FROM `tab{CALL}` cl
  WHERE cl.type = 'Outgoing' AND cl.creation >= CAST(%(lo)s AS DATE) - INTERVAL 1 HOUR
    AND cl.creation < CAST(%(hi)s AS DATE) + INTERVAL 1 DAY AND cl.caller IN %(users)s
),
changes AS (
  SELECT x.*, LEAD(x.t) OVER (PARTITION BY x.user, x.line, x.d ORDER BY x.t) AS nxt FROM (
    SELECT sd.user, sd.line, sd.d, a.creation AS t, a.status, a.source, 0 AS carried, a.name AS src
    FROM shift_days sd JOIN `tab{LOG}` a
      ON a.user = sd.user AND a.event = 'Check-in' AND a.creation >= TIMESTAMP(sd.d) AND a.creation < TIMESTAMP(sd.d) + INTERVAL 1 DAY
    UNION ALL
    -- The status the day starts with is the newest check-in row before it, and that row is its source.
    SELECT sd.user, sd.line, sd.d, TIMESTAMP(sd.d), la.status, 'Carried over', 1, la.name
    FROM shift_days sd JOIN `tab{LOG}` la ON la.name = (
      SELECT a.name FROM `tab{LOG}` a WHERE a.user = sd.user AND a.event = 'Check-in' AND a.creation < TIMESTAMP(sd.d)
      ORDER BY a.creation DESC LIMIT 1)
  ) x
),
spells AS (
  SELECT ROW_NUMBER() OVER (ORDER BY c.user, c.line, c.t) AS sid, c.user, c.line, c.d, c.status, c.source, c.src,
         GREATEST(c.t, sd.ss) AS s, LEAST(COALESCE(c.nxt, sd.se), sd.se, %(now)s) AS e
  FROM changes c JOIN shift_days sd ON sd.user = c.user AND sd.line = c.line AND sd.d = c.d
  WHERE c.status IN ('Active', 'Away')
),
gaps AS (
  SELECT a.user, a.last_active_at AS gs, MIN(a.creation) AS ge, MIN(a.name) AS src
  FROM `tab{LOG}` a
  WHERE a.event = 'Away' AND a.creation >= %(lo)s AND a.last_active_at IS NOT NULL AND a.user IN %(users)s
  GROUP BY a.user, a.last_active_at
),
idle_raw AS (
  SELECT sp.sid, sp.user, sp.line, sp.d, GREATEST(g.gs, sp.s) AS s, LEAST(g.ge, sp.e) AS e, g.src
  FROM spells sp JOIN gaps g ON g.user = sp.user AND g.gs < sp.e AND g.ge > sp.s
  WHERE sp.status = 'Active' AND sp.e > sp.s
),
-- Two tabs can report returns seconds apart and leave overlapping gaps; overlapping gaps merge into one idle stretch.
idle_marked AS (
  SELECT r.*, IF(r.s <= MAX(r.e) OVER (PARTITION BY r.sid ORDER BY r.s, r.e ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0, 1) AS opens
  FROM idle_raw r
),
idle AS (
  SELECT sid, user, line, d, MIN(s) AS s, MAX(e) AS e, MIN(src) AS src
  FROM (SELECT m.*, SUM(m.opens) OVER (PARTITION BY m.sid ORDER BY m.s, m.e ROWS UNBOUNDED PRECEDING) AS grp FROM idle_marked m) x
  GROUP BY sid, user, line, d, grp
),
working AS (
  SELECT sp.user, sp.line, sp.d, COALESCE(LAG(i.e) OVER (PARTITION BY sp.sid ORDER BY i.s), sp.s) AS s, i.s AS e, sp.src
  FROM spells sp JOIN idle i ON i.sid = sp.sid
  UNION ALL
  SELECT sp.user, sp.line, sp.d, COALESCE((SELECT MAX(i.e) FROM idle i WHERE i.sid = sp.sid), sp.s) AS s, sp.e, sp.src
  FROM spells sp WHERE sp.status = 'Active'
),
events AS (
  SELECT user, line, d, 'Working' AS kind, s, e, NULL AS talk_sec, '' AS detail, '{LOG}' AS src_doctype, src AS src_name FROM working WHERE e > s
  UNION ALL
  SELECT user, line, d, 'Idle', s, e, NULL, 'No CRM touch and no CRM call', '{LOG}', src FROM idle WHERE e > s
  UNION ALL
  SELECT user, line, d, 'Break', s, e, NULL, CONCAT('Set Away by ', source), '{LOG}', src FROM spells WHERE status = 'Away' AND e > s
  UNION ALL
  SELECT c.user, c.line, c.d, CASE c.status WHEN 'Active' THEN 'Set Active' WHEN 'Away' THEN 'Set Away' ELSE 'Checked out' END,
         c.t, NULL, NULL, CONCAT('By ', c.source), '{LOG}', c.src
  FROM changes c WHERE c.carried = 0
  UNION ALL
  -- A lead received is one the person won from a pool of this line, inside the shift.
  SELECT sd.user, sd.line, sd.d, 'Lead received', f.assigned_at, NULL, NULL, CONCAT(f.pool, ' · ', f.lead), '{TODO}', f.todo
  FROM shift_days sd JOIN `tab{lead_allocation_fact.TARGET}` f
    ON f.user = sd.user AND f.won = 1 AND f.assigned_at >= sd.ss AND f.assigned_at < sd.se
   AND CONCAT_WS('|', COALESCE(f.grain_vertical, ''), COALESCE(f.grain_group, ''), COALESCE(f.grain_program, '')) = sd.line
  UNION ALL
  SELECT sd.user, sd.line, sd.d, 'Call', COALESCE(cl.start_time, cl.creation), cl.end_time,
         IF(cl.status = 'Completed', cl.duration, 0), CONCAT(cl.status, ' · ', COALESCE(cl.reference_docname, '')), '{CALL}', cl.name
  FROM shift_days sd JOIN calls cl
    ON cl.caller = sd.user AND COALESCE(cl.start_time, cl.creation) >= sd.ss AND COALESCE(cl.start_time, cl.creation) < sd.se
  UNION ALL
  -- A shift day that has opened with no Active at all (carried over counts) and no leave covering the line: not checked in.
  SELECT sd.user, sd.line, sd.d, 'Not checked in', sd.ss, NULL, NULL, 'No check-in this shift', '{MEMBER}', sd.member
  FROM shift_days sd JOIN `tabUser` us ON us.name = sd.user AND us.enabled = 1
  WHERE sd.paused = 0 AND sd.ss <= %(now)s
    AND NOT EXISTS (SELECT 1 FROM changes c WHERE c.user = sd.user AND c.line = sd.line AND c.d = sd.d AND c.status = 'Active')
    AND NOT EXISTS (
      SELECT 1 FROM `tabTatva User Leave` l WHERE l.user = sd.user AND l.from_date <= sd.d AND l.to_date >= sd.d
        AND COALESCE(l.grain_vertical, '') IN ('', COALESCE(sd.grain_vertical, ''))
        AND COALESCE(l.grain_group, '') IN ('', COALESCE(sd.grain_group, ''))
        AND COALESCE(l.grain_program, '') IN ('', COALESCE(sd.grain_program, '')))
),
firsts AS (SELECT user, line, d, MIN(t) AS first_in FROM changes WHERE status = 'Active' AND carried = 0 GROUP BY user, line, d)
SELECT {key} AS grain_key, ev.user, COALESCE(NULLIF(u.full_name, ''), ev.user) AS sales_user, ev.d AS day,
       sd.grain_vertical, sd.grain_group, sd.grain_program, ev.src_doctype AS source_doctype, ev.src_name AS source_name,
       ev.kind, ev.s AS from_at, ev.e AS to_at,
       -- A moment has no end, and Frappe numeric columns are NOT NULL, so it stores 0.
       COALESCE(ROUND(TIMESTAMPDIFF(SECOND, ev.s, ev.e) / 60, 1), 0) AS minutes,
       ROUND(IF(ev.kind = 'Working', TIMESTAMPDIFF(SECOND, ev.s, ev.e), 0) / 3600, 4) AS working_h,
       ROUND(IF(ev.kind = 'Idle', TIMESTAMPDIFF(SECOND, ev.s, ev.e), 0) / 3600, 4) AS idle_h,
       ROUND(IF(ev.kind = 'Idle', TIMESTAMPDIFF(SECOND, ev.s, ev.e), 0) / 60, 1) AS idle_min,
       ev.kind = 'Idle' AS idle_gaps,
       ROUND(IF(ev.kind = 'Break', TIMESTAMPDIFF(SECOND, ev.s, ev.e), 0) / 3600, 4) AS break_h,
       ev.kind = 'Break' AS breaks, ev.kind = 'Lead received' AS leads, ev.kind = 'Call' AS calls,
       COALESCE(ev.talk_sec, 0) > 0 AS connected, ROUND(COALESCE(ev.talk_sec, 0) / 60, 1) AS talk_min,
       -- Day facts sit on the one row they come from, so a min or max over a day finds them and a drill-down stays clean.
       IF(ev.kind = 'Set Active' AND ev.s = f.first_in, TIME_FORMAT(ev.s, '%%H:%%i'), NULL) AS first_check_in,
       IF(ev.kind = 'Set Active' AND ev.s = f.first_in, GREATEST(TIMESTAMPDIFF(MINUTE, sd.ss, ev.s), 0), 0) AS minutes_late,
       IF(ev.kind = 'Checked out', TIME_FORMAT(ev.s, '%%H:%%i'), NULL) AS checked_out,
       ev.detail
FROM events ev
JOIN shift_days sd ON sd.user = ev.user AND sd.line = ev.line AND sd.d = ev.d
LEFT JOIN firsts f ON f.user = ev.user AND f.line = ev.line AND f.d = ev.d
LEFT JOIN `tabUser` u ON u.name = ev.user"""


def rewrite(run, users, lo, hi):
	"""Recompute `users` on days `lo`..`hi`, the person-day being the unit: upsert every row, then drop the unit's rows no longer produced."""
	written = 0
	for start in range(0, len(users), USER_CHUNK):
		chunk = tuple(users[start:start + USER_CHUNK])
		sql = select_sql(days=(getdate(hi) - getdate(lo)).days)
		rows = frappe.db.sql(sql, {"users": chunk, "lo": lo, "hi": hi, "now": run.started}, as_dict=True)  # sqli-ok: the model's own SQL, values bound
		written += run.write(TARGET, rows)
		frappe.db.delete(TARGET, {"user": ("in", chunk), "day": ("between", (lo, hi)),
		                          "grain_key": ("not in", [r.grain_key for r in rows] or [""])})
		run.checkpoint()
	return written


def touched_days(since, until):
	"""Each person's earliest day changed since the watermark: new check-in rows, changed calls, new pool ToDos, and their deletes."""
	first = {}

	def mark(user, moment):
		if user and moment:
			day = getdate(moment)
			first[user] = min(first.get(user, day), day)

	for row in frappe.get_all(LOG, fields=["user", "creation"], filters={"creation": ("between", (since, until))}):
		mark(row.user, row.creation)
	for row in frappe.get_all(CALL, fields=["caller", "start_time", "creation"],
	                          filters={"modified": ("between", (since, until)), "type": "Outgoing"}):
		mark(row.caller, row.start_time or row.creation)
	for row in frappe.get_all(TODO, fields=["allocated_to", "creation"],
	                          filters={"creation": ("between", (since, until)), "reference_type": "CRM Lead", "assignment_rule": ("is", "set")}):
		mark(row.allocated_to, row.creation)
	deleted = 0
	for doctype, who, when in ((LOG, "user", "creation"), (CALL, "caller", "start_time"), (TODO, "allocated_to", "creation")):
		for _name, record in facts.deleted_since(doctype, since, until):
			mark(record.get(who), record.get(when) or record.get("creation"))
			deleted += 1
	return first, deleted


def refresh(run):
	"""Recompute each person from their earliest touched day, and today for everyone; the window bounds only a first load or Rebuild."""
	today = getdate(run.started)
	people = sorted({r.user for r in frappe.db.sql(people_sql(), as_dict=True)})  # sqli-ok: constant SQL
	if not run.watermark:
		return {"changed": len(people), "written": rewrite(run, people, getdate(facts.read_from(run)), today)}
	first, deleted = touched_days(facts.read_from(run), run.started)
	written, by_day = 0, {}
	for user, day in first.items():
		if user in people:
			by_day.setdefault(min(day, today), []).append(user)
	for lo, users in sorted(by_day.items()):
		written += rewrite(run, sorted(users), lo, today)
	# Everyone else recomputes only today, whose open stretches grow with the clock.
	written += rewrite(run, sorted(set(people) - {u for users in by_day.values() for u in users}), today, today)
	return {"changed": sum(len(users) for users in by_day.values()), "written": written, "deleted": deleted}
