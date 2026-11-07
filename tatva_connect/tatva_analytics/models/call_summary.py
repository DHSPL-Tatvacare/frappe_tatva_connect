"""One row per agent over the Call Facts of its window: the toolkit's Agent risk sheet, scored against the agent's team."""
import frappe
from frappe.utils import add_days

from tatva_connect.tatva_analytics import facts
from tatva_connect.tatva_analytics.models import call_fact

JOB = "call_summary"
TARGET = "Tatva Call Summary"
AFTER = call_fact.JOB
# The days of Call Facts each agent is scored over, the same as the fact's own first load.
WINDOW_DAYS = call_fact.WINDOW_DAYS
# One snapshot per agent per day: every run that day updates it, and earlier days are kept.
PERIOD = "day"
PROBE = {"snapshot": "1900-01-01"}

# The toolkit's settings, verbatim.
CUTOFF_MIN, LONG_CALL_S, MIN_DIALS = 30, 300, 40
# key, label, attention threshold. Toolkit tests the CRM cannot feed (closed leads, call notes, lead-stage checks) are left out.
TESTS = [("flash", "Flash dials", 0.15), ("early", "Early cuts (3-9 s)", 0.25), ("ghost", "Own phone not picked", 0.10),
         ("redial", "Redial loops", 0.12), ("junk", "Junk / internal numbers", 0.02), ("notcrm", "Not a CRM lead", 0.15),
         ("otherowner", "Someone else's leads", 0.20), ("burst", "Rushed bursts", 0.10), ("cutoff", "End-of-day spike", 0.12),
         ("short", "Short connects", 0.30), ("conc", "Talk-time concentration", 0.25), ("norec", "Missing recordings", 0.05)]


def select_sql():
	"""Per-agent metrics, then each test as a z-score against the agent's team (median, MAD x 1.4826)."""
	short = call_fact.SHORT_CONNECT_S
	ranked = f"""SELECT w.*, DENSE_RANK() OVER (PARTITION BY w.who ORDER BY w.number_talk DESC, w.num) AS number_rank,
			FIRST_VALUE(w.program) OVER (PARTITION BY w.who ORDER BY w.program_dials DESC, w.program) AS team,
			ROW_NUMBER() OVER (PARTITION BY w.who, w.day, w.num, w.f_junk ORDER BY w.started) AS day_number_seq,
			ROW_NUMBER() OVER (PARTITION BY w.who, w.num ORDER BY w.started) AS number_seq,
			IF(w.day_dials >= 20, 1, 0) AS in_shift,
			IF(w.day_dials >= 20 AND w.started >= w.last_dial - INTERVAL {CUTOFF_MIN} MINUTE, 1, 0) AS f_cutoff
		FROM (SELECT c.*, SUM(IF(c.answered = 1, c.talk, 0)) OVER (PARTITION BY c.who, c.num) AS number_talk,
				COUNT(*) OVER (PARTITION BY c.who, c.program) AS program_dials,
				SUM(IF(c.answered = 1 AND c.talk >= {LONG_CALL_S}, 1, 0)) OVER (PARTITION BY c.who, c.num) AS number_long,
				MAX(c.started) OVER (PARTITION BY c.who, c.day) AS last_dial, COUNT(*) OVER (PARTITION BY c.who, c.day) AS day_dials
			FROM (SELECT f.*, COALESCE(f.agent_user, '') AS who FROM `tab{call_fact.TARGET}` f WHERE f.started >= %(lo)s) c) w"""
	metrics = f"""SELECT MAX(c.agent) AS agent, MAX(c.agent_user) AS agent_user, MAX(c.team) AS team, COUNT(*) AS dials, COUNT(DISTINCT c.day) AS days,
			SUM(c.answered) AS answered, SUM(c.valid) AS valid, ROUND(SUM(IF(c.answered = 1, c.talk, 0)) / 60, 1) AS talk_min,
			SUM(IF(c.f_junk = 0 AND c.day_number_seq = 1, 1, 0)) AS unique_day_numbers,
			SUM(IF(c.number_seq = 1 AND c.number_long >= 3, 1, 0)) AS long_same, 1 - SUM(c.valid) / COUNT(*) AS inflation,
			IF(SUM(c.ring_known) > 0 AND SUM(1 - c.answered) > 0, SUM(c.f_flash) / SUM(1 - c.answered), NULL) AS flash,
			IF(SUM(c.ring_known) > 0 AND SUM(1 - c.answered) > 0, SUM(c.f_early) / SUM(1 - c.answered), NULL) AS early,
			IF(SUM(c.dialled <> '') > 0, AVG(c.f_ghost), NULL) AS ghost,
			AVG(c.f_redial) AS redial, AVG(c.f_junk) AS junk, AVG(c.f_notcrm) AS notcrm,
			IF(MAX(c.agent_is_owner) = 1, AVG(c.f_otherowner), NULL) AS otherowner,
			AVG(c.f_burst) AS burst, AVG(c.f_sysfail) AS sysfail, AVG(c.f_netend) AS netend,
			IF(SUM(c.in_shift) > 0, SUM(c.f_cutoff) / SUM(c.in_shift), NULL) AS cutoff,
			IF(SUM(c.answered) > 0, SUM(c.f_short) / SUM(c.answered), NULL) AS short,
			IF(SUM(c.answered = 1 AND c.talk >= {short}) > 0, SUM(c.f_norec) / SUM(c.answered = 1 AND c.talk >= {short}), NULL) AS norec,
			IF(SUM(c.answered) >= 50 AND COUNT(DISTINCT c.day) >= 5,
			   SUM(IF(c.answered = 1 AND c.number_rank <= 3, c.talk, 0)) / NULLIF(SUM(IF(c.answered = 1, c.talk, 0)), 0), NULL) AS conc
		FROM ({ranked}) c GROUP BY c.who"""
	keys = [k for k, _, _ in TESTS]
	joined = f"""SELECT m.*, IF(u.enabled = 0, 1, 0) AS has_left,
			IF(m.dials >= {MIN_DIALS} AND m.agent_user IS NOT NULL AND COALESCE(u.enabled, 1) = 1, 1, 0) AS scored
		FROM ({metrics}) m LEFT JOIN `tabUser` u ON u.name = m.agent_user"""
	med = ", ".join(f"PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY IF(j.scored = 1, j.{k}, NULL)) OVER (PARTITION BY j.team) AS med_{k}, "
	                f"COUNT(IF(j.scored = 1, j.{k}, NULL)) OVER (PARTITION BY j.team) AS n_{k}" for k in keys)
	mad = ", ".join(f"1.4826 * PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY IF(d.scored = 1, ABS(d.{k} - d.med_{k}), NULL)) "
	                f"OVER (PARTITION BY d.team) AS mad_{k}" for k in keys)
	z = ", ".join(f"IF(e.n_{k} >= 3, (e.{k} - e.med_{k}) / GREATEST(COALESCE(e.mad_{k}, 0), {thr} / 3, 0.01), NULL) AS z_{k}"
	              for k, _, thr in TESTS)
	pts = ", ".join(f"IF(s.scored = 1 AND s.{k} >= {thr} AND s.z_{k} >= 2, LEAST(s.z_{k}, 6), 0) AS p_{k}" for k, _, thr in TESTS)
	risk = " + ".join(f"p.p_{k}" for k in keys)
	zmax = "GREATEST(" + ", ".join(f"COALESCE(p.z_{k}, -99)" for k in keys) + ")"
	flags = ", ".join(f"IF(p.p_{k} > 0, {frappe.db.escape(label)}, NULL)" for k, label, _ in TESTS)
	# A test that did not run is stored as 0: Frappe numeric columns are NOT NULL; scoring above already ran on the NULLs.
	rates = ", ".join(f"ROUND(COALESCE(p.{k}, 0) * 100, 1) AS {k}" for k in [*keys, "sysfail", "netend"])
	return f"""SELECT {facts.grain_key("COALESCE(p.agent_user, '')", "%(snapshot)s")} AS grain_key, p.agent,
			COALESCE(p.agent_user, '') AS agent_user, %(snapshot)s AS snapshot, 1 AS is_latest, p.team, p.has_left, p.scored,
			ROUND({risk}, 1) AS risk,
			CASE WHEN p.scored = 1 AND (({risk}) >= 10 OR (({zmax}) >= 4 AND ({risk}) > 0)) THEN 'High'
			     WHEN p.scored = 1 AND ({risk}) > 0 THEN 'Medium' WHEN p.has_left = 1 THEN 'Left the team'
			     WHEN p.scored = 0 THEN 'Too few dials' ELSE 'Low' END AS tier,
			CONCAT_WS(', ', {flags}) AS flags, p.dials, p.valid, ROUND(p.inflation * 100, 1) AS inflation, p.days,
			ROUND(p.dials / p.days, 1) AS dials_per_day, ROUND(p.valid / p.days, 1) AS valid_per_day,
			ROUND(p.unique_day_numbers / p.days, 1) AS unique_per_day, p.answered, p.talk_min, p.long_same, {rates}
		FROM (SELECT s.*, {pts} FROM (SELECT e.*, {z} FROM (SELECT d.*, {mad} FROM (SELECT j.*, {med} FROM ({joined}) j) d) e) s) p"""


def refresh(run):
	"""Score every agent over the Call Facts of the last `window_days` into today's snapshot, which alone reads as latest."""
	snapshot = facts.period_start(run.started, PERIOD)
	rows = frappe.db.sql(select_sql(), {"lo": add_days(run.started, -run.window_days), "snapshot": snapshot}, as_dict=True)  # sqli-ok: the model's own SQL, values bound
	facts.clear_latest(TARGET)
	return {"changed": len(rows), "written": run.write(TARGET, rows)}
