"""One row per lead a pool assigned and per pool member considered for it: who got it, why each other member could not take it, and their fair share."""

import frappe

from tatva_connect.lead.assignment import ASSIGNED, ITEM, PAIR, SKIPPED
from tatva_connect.lead.assignment_rule import AWAY, CAP, UNTOUCHED_LIMIT, WEIGHTED, WHY_NOT
from tatva_connect.tatva_analytics import facts

JOB = "lead_allocation_fact"
TARGET = "Tatva Lead Allocation Fact"
WINDOW_DAYS = 30
# Skipped yet checked in at the assignment: they share in that lead's fair share.
CHECKED_IN = (AWAY, UNTOUCHED_LIMIT, CAP)
# The step row that names who was skipped is written after its ToDo, within this.
STEP_LAG = "INTERVAL 5 MINUTE"
PROBE = {"since": "1900-01-01", "until": "1900-01-01"}
# Fact rows are updated or deleted by ToDo this many at a time.
CHUNK = 1000


def quoted(values):
	return ", ".join(frappe.db.escape(v) for v in values)


def select_sql():
	"""Every pool ToDo created in %(since)s..%(until)s, times every member of its pool at that moment.

	An assignment is a pool ToDo, matched to the step row that wrote `assigned to … · skipped: …`; with no step row it
	counts for its winner alone. A member's share is their weight over the weight of everyone checked in at that moment:
	the winner, anyone the pool could have picked, and anyone skipped only for being idle, at the unworked-lead limit or
	at the daily cap. First means the lead's first ToDo from a pool of the same strategy, ever."""
	codes = " UNION ALL ".join(f"SELECT {frappe.db.escape(code)} AS code" for code in WHY_NOT)
	weighted = quoted(WEIGHTED)
	# The step row's own wording (lead/assignment.py): the winner follows ASSIGNED, each skipped member opens with SKIPPED or ITEM.
	assigned, skipped, item, pair = (frappe.db.escape(t) for t in (ASSIGNED, SKIPPED, ITEM, PAIR))
	member = "CONCAT({sep}, m.user, " + pair + ", c.code)"
	return f"""WITH pools AS (
  SELECT name, rule, grain_vertical, grain_group, grain_program FROM `tabAssignment Rule` WHERE document_type = 'CRM Lead'
),
-- DISTINCT makes MariaDB read the window's step rows once and join them, never once per ToDo (subject_name has no index).
given AS (
  SELECT DISTINCT s.subject_name AS lead, s.creation, s.detail,
         SUBSTRING_INDEX(SUBSTRING(s.detail, {len(ASSIGNED) + 1}), {skipped}, 1) AS winner
  FROM `tabCRM Workflow Step Log` s
  WHERE s.creation >= %(since)s AND s.creation < %(until)s + {STEP_LAG} AND s.node_type IN ('Distribute', 'Assign')
    AND s.detail LIKE CONCAT({assigned}, '%%')
),
draws AS (
  SELECT * FROM (
    SELECT t.name AS todo, t.creation AS at, t.assignment_rule AS pool, t.reference_name AS lead, t.allocated_to AS winner,
           t.status, g.detail, ROW_NUMBER() OVER (PARTITION BY t.name ORDER BY g.creation) AS rn,
           NOT EXISTS (SELECT 1 FROM `tabToDo` e JOIN pools ep ON ep.name = e.assignment_rule AND ep.rule = p.rule
                       WHERE e.reference_type = 'CRM Lead' AND e.reference_name = t.reference_name AND e.creation < t.creation) AS first_draw
    FROM `tabToDo` t
    JOIN pools p ON p.name = t.assignment_rule
    LEFT JOIN given g ON g.lead = t.reference_name AND g.winner = t.allocated_to
                     AND g.creation >= t.creation AND g.creation < t.creation + {STEP_LAG}
    WHERE t.reference_type = 'CRM Lead' AND t.creation >= %(since)s AND t.creation < %(until)s
  ) x WHERE rn = 1
),
members AS (
  SELECT m.parent AS pool, m.user, m.creation AS joined, IF(p.rule IN ({weighted}), COALESCE(NULLIF(m.weight, 0), 1), 1) AS weight
  FROM `tabAssignment Rule User` m
  JOIN pools p ON p.name = m.parent
  WHERE m.parenttype = 'Assignment Rule' AND m.parentfield = IF(p.rule IN ({weighted}), 'weighted_users', 'users')
    AND m.parent IN (SELECT pool FROM draws)
),
codes AS ({codes}),
looked AS (
  SELECT dr.todo, dr.at, dr.pool, dr.lead, dr.first_draw, dr.status, m.user, m.weight, dr.detail IS NOT NULL AS recorded,
         m.user = dr.winner AS won,
         IF(m.user = dr.winner, 'won',
            COALESCE((SELECT c.code FROM codes c WHERE LOCATE({member.format(sep=skipped)}, dr.detail) > 0
                      OR LOCATE({member.format(sep=item)}, dr.detail) > 0 LIMIT 1), '')) AS why
  FROM draws dr JOIN members m ON m.pool = dr.pool AND m.joined <= dr.at
  UNION ALL
  -- A winner who has since left the pool still counts, at weight 1.
  SELECT dr.todo, dr.at, dr.pool, dr.lead, dr.first_draw, dr.status, dr.winner, 1, dr.detail IS NOT NULL, 1, 'won'
  FROM draws dr LEFT JOIN members m ON m.pool = dr.pool AND m.user = dr.winner AND m.joined <= dr.at
  WHERE m.user IS NULL
),
seats AS (
  SELECT l.*, (l.why = 'won' OR (l.recorded AND (l.why = '' OR l.why IN ({quoted(CHECKED_IN)})))) AS in_share FROM looked l
)
SELECT {facts.grain_key('s.todo', 's.user')} AS grain_key, s.todo, s.at AS assigned_at, DATE(s.at) AS day, s.pool,
       p.grain_vertical, p.grain_group, p.grain_program, s.lead, s.user, COALESCE(NULLIF(u.full_name, ''), s.user) AS sales_user,
       s.weight, s.why, s.in_share,
       -- In DOUBLE: MariaDB divides decimals to 4 places, and those roundings add up across an assignment's seats.
       CAST(IF(s.in_share, s.weight, 0) AS DOUBLE) / SUM(IF(s.in_share, s.weight, 0)) OVER (PARTITION BY s.todo) AS share,
       s.won, s.first_draw, s.won AND s.status = 'Cancelled' AS moved_off
FROM seats s
JOIN pools p ON p.name = s.pool
LEFT JOIN `tabUser` u ON u.name = s.user"""


def refresh(run):
	"""Assignments made since the watermark, then ToDos changed or deleted since it; the window bounds only a first load or Rebuild."""
	since = facts.read_from(run)
	rows = frappe.db.sql(select_sql(), {"since": since, "until": run.started}, as_dict=True)  # sqli-ok: the model's own SQL, values bound
	written = run.write(TARGET, rows)
	run.checkpoint()
	if not run.watermark:
		return {"changed": len({r.todo for r in rows}), "written": written}
	# A cancelled ToDo moves the lead off its winner: read by ToDo's modified index with a plain SELECT, which locks nothing.
	todo = frappe.qb.DocType("ToDo")
	changed = (frappe.qb.from_(todo).select(todo.name, todo.status).where(todo.reference_type == "CRM Lead")
	           .where(todo.modified > since).where(todo.modified <= run.started).run(as_dict=True))
	for moved_off in (1, 0):
		names = [t.name for t in changed if (t.status == "Cancelled") == bool(moved_off)]
		for start in range(0, len(names), CHUNK):
			fact = frappe.qb.DocType(TARGET)
			(frappe.qb.update(fact).set(fact.moved_off, moved_off).where(fact.todo.isin(names[start:start + CHUNK]))
			 .where(fact.won == 1).where(fact.moved_off != moved_off).run())
	deleted = [name for name, _record in facts.deleted_since("ToDo", since, run.started)]
	for start in range(0, len(deleted), CHUNK):
		frappe.db.delete(TARGET, {"todo": ("in", deleted[start:start + CHUNK])})
	return {"changed": len({r.todo for r in rows}) + len(changed), "written": written, "deleted": len(deleted)}
