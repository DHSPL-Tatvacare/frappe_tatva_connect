"""One row per outbound call, with the dial-audit toolkit's per-call flags (dial_audit.py, checksum 5575be39cf6c4d29)."""
from datetime import timedelta

import frappe
from frappe.utils import get_datetime

from tatva_connect.tatva_analytics import facts

JOB = "call_fact"
TARGET = "Tatva Call Fact"
WINDOW_DAYS = 7
MEDIA = ("Acefone",)

# The toolkit's settings, verbatim.
FLASH_S, EARLY_S, VALID_RING_S = 3, 10, 20
REDIAL_MIN, BURST_N, BURST_S, SHORT_CONNECT_S = 15, 8, 300, 10
END_PARTY = {"call disconnected by caller": "agent", "cancel": "agent", "call disconnected by callee": "customer",
             "busy": "customer", "noanswer": "no answer (timeout)", "calls dropped": "dropped",
             "calls dropped because no one picked up the call": "no answer (timeout)"}
SYSTEM_FAIL = ("agent_is_busy", "agent is offline", "call_product_not_found", "agent_extension_in_on_other_call",
               "congestion in network")

# A call's flags reach its neighbours this far: redial looks two dials back within 15 min, a burst 5 min either side.
REACH = timedelta(minutes=2 * REDIAL_MIN)
# Changed calls closer than this are recomputed in one pass, and no pass spans more than a day, so a first load or a rebuild commits day by day.
GAP = timedelta(hours=1)
SPAN = timedelta(days=1)

# Where the CRM keeps a number that is ours: agent phones, extensions, follow-me phones, DIDs, caller IDs.
OUR_NUMBERS = [("tabUser", "mobile_no"), ("tabCRM Telephony Agent", "mobile_no"),
               ("tabCRM Telephony Phone", "number"), ("tabCRM Telephony Agent Extension", "extension"),
               ("tabCRM Telephony Agent Extension", "follow_me_number"), ("tabCRM Telephony Routing DID", "did_number"),
               ("tabCRM Telephony Account", "agent_number"), ("tabCRM Telephony Account", "caller_id")]


def last10(column):
	return f"RIGHT(REGEXP_REPLACE(COALESCE({column}, ''), '[^0-9]', ''), 10)"


def quoted(values):
	return ", ".join(frappe.db.escape(v) for v in values)


def select_sql():
	"""The per-call query over %(lo)s..%(hi)s for %(agents)s; rows outside %(write_lo)s..%(write_hi)s are context only."""
	party = " ".join(f"WHEN {frappe.db.escape(k)} THEN {frappe.db.escape(v)}" for k, v in END_PARTY.items())
	internal = " UNION ".join(f"SELECT {last10(col)} AS n FROM `{table}` WHERE COALESCE({col}, '') <> ''" for table, col in OUR_NUMBERS)
	most_of_one_digit = "GREATEST(" + ", ".join(f"LENGTH(b.num) - LENGTH(REPLACE(b.num, '{d}', ''))" for d in range(10)) + ")"
	base = f"""SELECT c.name, c.custom_provider_call_id AS call_id, l.status AS stage, c.start_time AS started, DATE(c.start_time) AS day,
			c.caller AS agent_user, COALESCE(c.caller, '') AS who, COALESCE(u.full_name, c.caller, 'Unattributed') AS agent, {last10('c.`to`')} AS num,
			l.name AS lead, l.lead_owner, COALESCE(l.custom_current_program, 'No lead') AS program,
			EXISTS (SELECT 1 FROM `tabCRM Lead` o WHERE o.lead_owner = c.caller) AS agent_is_owner,
			IF(l.lead_owner IS NOT NULL AND l.lead_owner <> c.caller
			   AND NOT JSON_CONTAINS(COALESCE(NULLIF(l._assign, ''), '[]'), JSON_QUOTE(c.caller)), 1, 0) AS other_owner,
			c.telephony_medium AS medium, COALESCE(NULLIF(c.custom_call_type, ''), 'Unknown') AS call_type,
			c.status, IF(c.status = 'Completed' OR COALESCE(c.duration, 0) > 0, 1, 0) AS answered, COALESCE(c.duration, 0) AS talk,
			IF(c.custom_customer_dialled = 'Yes', c.custom_customer_ring_sec, NULL) AS ring,
			COALESCE(c.custom_hangup_reason, '') AS hangup_reason, LOWER(TRIM(COALESCE(c.custom_hangup_reason, ''))) AS rk,
			COALESCE(c.custom_customer_dialled, '') AS dialled,
			-- A recording the retry sweep stored later lives on its media row, not on the Call Log.
			IF(COALESCE(c.recording_url, '') <> '' OR m.recording_state = 'Stored', 1, 0) AS has_recording
		FROM `tabCRM Call Log` c
		LEFT JOIN `tabCRM Lead` l ON c.reference_doctype = 'CRM Lead' AND l.name = c.reference_docname
		LEFT JOIN `tabUser` u ON u.name = c.caller
		LEFT JOIN `tabCRM Call Media` m ON m.name = c.name
		WHERE c.creation >= %(lo)s - INTERVAL 1 HOUR AND c.start_time BETWEEN %(lo)s AND %(hi)s AND c.type = 'Outgoing'
		  AND c.telephony_medium IN %(media)s AND COALESCE(c.caller, '') IN %(agents)s"""
	# A number "works" when any call to it connected or rang normally: in this pass, or already stored.
	tagged = f"""SELECT b.*, CASE b.rk {party} ELSE '' END AS ended_by, IF(b.dialled = 'No' AND b.rk = '', 1, 0) AS f_ghost,
			IF(b.rk = 'invalid number dialed', 1, 0) AS netinv, IF(i.n IS NULL, 0, 1) AS f_internal,
			IF(b.lead IS NOT NULL, 1, 0) AS in_crm,
			IF(LENGTH(b.num) < 10 OR LEFT(b.num, 1) NOT IN ('6', '7', '8', '9') OR {most_of_one_digit} >= 8
			   OR b.num IN ('1234567890', '0123456789', '9876543210'), 1, 0) AS bad_number,
			IF(b.answered = 1 OR (b.rk <> 'invalid number dialed' AND COALESCE(b.ring, 0) >= 5), 1, 0) AS reaches,
			GREATEST(MAX(IF(b.answered = 1 OR (b.rk <> 'invalid number dialed' AND COALESCE(b.ring, 0) >= 5), 1, 0)) OVER (PARTITION BY b.num),
			         EXISTS (SELECT 1 FROM `tab{TARGET}` s WHERE s.num = b.num AND s.reaches = 1)) AS reached
		FROM ({base}) b LEFT JOIN ({internal}) i ON i.n = b.num"""
	flagged = f"""SELECT t.*, 1 - t.answered AS unanswered, IF(t.ring IS NULL, 0, 1) AS ring_known,
			IF(t.netinv = 1 AND t.reached = 0, 1, 0) AS f_netinvalid,
			IF((t.rk IN ({quoted(SYSTEM_FAIL)}) OR (t.netinv = 1 AND t.reached = 1)) AND NOT (t.netinv = 1 AND t.reached = 0), 1, 0) AS f_sysfail,
			IF(t.answered = 0 AND t.ended_by IN ('dropped', 'customer', 'no answer (timeout)'), 1, 0) AS f_netend
		FROM ({tagged}) t"""
	cuts = f"""SELECT f.*, IF(f.f_internal = 1 OR f.bad_number = 1 OR f.f_netinvalid = 1, 1, 0) AS f_junk,
			IF(f.f_sysfail = 1 OR f.f_netinvalid = 1 OR f.f_ghost = 1, 1, 0) AS not_dial,
			IF(f.answered = 0 AND f.ring < {FLASH_S} AND f.ended_by IN ('agent', '') AND f.f_sysfail = 0 AND f.f_netinvalid = 0 AND f.f_ghost = 0, 1, 0) AS f_flash,
			IF(f.answered = 0 AND f.ring >= {FLASH_S} AND f.ring < {EARLY_S} AND f.ended_by IN ('agent', '')
			   AND f.f_sysfail = 0 AND f.f_netinvalid = 0 AND f.f_ghost = 0, 1, 0) AS f_early,
			IF(f.answered = 1 AND f.talk < {SHORT_CONNECT_S}, 1, 0) AS f_short,
			IF(f.answered = 1 AND f.talk >= {SHORT_CONNECT_S} AND f.has_recording = 0, 1, 0) AS f_norec,
			IF(f.f_ghost = 0 AND f.f_sysfail = 0, 1, 0) AS is_real
		FROM ({flagged}) f"""
	# Redial loop: same agent and number within 15 min among dials that rang the customer; ONE retry after a fresh drop, busy or early cut is excused.
	lagged = f"""SELECT k.*,
			IF(LAG(k.started) OVER w IS NOT NULL AND k.started < LAG(k.started) OVER w + INTERVAL {REDIAL_MIN} MINUTE, 1, 0) AS within_,
			COALESCE(LAG(IF(k.f_netend = 1 OR k.f_early = 1, 1, 0)) OVER w, 0) AS prev_genuine
		FROM ({cuts}) k WINDOW w AS (PARTITION BY k.who, k.num, k.is_real ORDER BY k.started, k.name)"""
	redial = f"""SELECT g.*, IF(g.is_real = 1 AND g.within_ = 1 AND NOT (g.prev_genuine = 1 AND COALESCE(LAG(g.within_) OVER w, 0) = 0), 1, 0) AS f_redial
		FROM ({lagged}) g WINDOW w AS (PARTITION BY g.who, g.num, g.is_real ORDER BY g.started, g.name)"""
	valid = f"""SELECT r.*,
			IF(r.answered = 1 OR r.ring IS NULL OR r.ring >= {VALID_RING_S} OR r.f_netend = 1 OR r.f_early = 1, 1, 0) AS rang_ok,
			IF((r.answered = 1 OR r.ring IS NULL OR r.ring >= {VALID_RING_S} OR r.f_netend = 1 OR r.f_early = 1)
			   AND r.f_junk = 0 AND r.f_redial = 0 AND r.f_ghost = 0 AND r.f_sysfail = 0, 1, 0) AS valid,
			UNIX_TIMESTAMP(r.started) AS ts,
			COUNT(*) OVER (PARTITION BY r.who, r.f_sysfail ORDER BY UNIX_TIMESTAMP(r.started) RANGE BETWEEN {BURST_S} PRECEDING AND CURRENT ROW) AS burst_back
		FROM ({redial}) r"""
	# The toolkit's REASON_COLS, in its order and words; the note reasons and Closed lead have no CRM source.
	shortring = f"v.answered = 0 AND v.ring >= {EARLY_S} AND v.ring < {VALID_RING_S} AND v.ended_by = 'agent' AND v.not_dial = 0"
	reasons = ", ".join(f"IF({cond}, {frappe.db.escape(label)}, NULL)" for cond, label in [
		("v.f_ghost = 1", "Own phone not picked"), ("v.f_netinvalid = 1", "Network: invalid number"), ("v.f_flash = 1", "Flash dial"),
		("v.f_early = 1", "Early cut (possible busy tone)"), ("v.f_redial = 1", "Redial loop"), ("v.f_internal = 1", "Internal number"),
		("(v.bad_number = 1 OR v.f_netinvalid = 1) AND v.f_internal = 0", "Invalid number"),
		("v.in_crm = 0 AND v.f_junk = 0", "Not in CRM"), ("v.agent_is_owner = 1 AND v.other_owner = 1", "Other agent's lead"),
		("v.f_short = 1", "Short connect"), ("v.f_norec = 1", "No recording"), (shortring, f"Cut at {EARLY_S}-{VALID_RING_S - 1}s")])
	burst = f"""MAX(IF(v.burst_back >= {BURST_N}, 1, 0)) OVER (PARTITION BY v.who, v.f_sysfail ORDER BY v.ts
			RANGE BETWEEN CURRENT ROW AND {BURST_S} FOLLOWING)"""
	return f"""SELECT * FROM (SELECT {facts.grain_key("v.name")} AS grain_key, v.name AS call_log, v.call_id, v.started, v.day, v.agent, v.agent_user, v.program, v.call_type, v.medium,
			v.num, v.lead, v.lead_owner, v.stage, v.in_crm, v.agent_is_owner, v.status, v.answered, v.talk, COALESCE(v.ring, 0) AS ring,
			v.hangup_reason, v.ended_by, v.dialled, v.has_recording, v.reaches, v.ring_known, v.is_real,
			v.f_flash, v.f_early, IF(v.rang_ok = 0 AND v.f_junk = 0 AND v.f_ghost = 0 AND v.f_sysfail = 0 AND v.f_flash = 0, 1, 0) AS f_cut,
			v.f_ghost, v.f_sysfail, v.f_netinvalid, v.f_internal, v.f_junk, v.f_redial, v.f_netend, v.f_short, v.f_norec,
			IF(v.in_crm = 0 AND v.f_junk = 0, 1, 0) AS f_notcrm, IF(v.agent_is_owner = 1 AND v.other_owner = 1, 1, 0) AS f_otherowner,
			IF(v.f_sysfail = 0 AND v.valid = 0 AND {burst} = 1, 1, 0) AS f_burst,
			IF(v.answered = 1 AND v.talk >= {SHORT_CONNECT_S}, 1, 0) AS f_conv, IF(v.ring < {EARLY_S}, 1, 0) AS quick, v.valid,
			CASE WHEN v.f_junk = 1 THEN 'Junk / internal number' WHEN v.f_ghost = 1 THEN 'Own phone not picked'
			     WHEN v.f_sysfail = 1 THEN 'System / network failure' WHEN v.f_flash = 1 THEN 'Flash dial'
			     WHEN v.rang_ok = 0 THEN 'Cut at {EARLY_S}-{VALID_RING_S - 1}s' WHEN v.f_redial = 1 THEN 'Repeat within {REDIAL_MIN} min'
			     ELSE '' END AS why_invalid,
			CONCAT_WS(', ', {reasons}) AS reasons
		FROM ({valid}) v) out_ WHERE out_.started BETWEEN %(write_lo)s AND %(write_hi)s"""


def refresh(run):
	"""Recompute the calls changed since the watermark and the neighbours their flags reach; the window bounds only a first load or Rebuild."""
	since = facts.read_from(run)
	# A call changes on its own row or on its media row, where the retry sweep stores audio Acefone publishes after the hangup.
	stored = frappe.get_all("CRM Call Media", filters=[["modified", ">", since], ["modified", "<=", run.started]], pluck="name")
	# A first load or Rebuild reads the window; after that any changed call counts, however old (a recording can land days later).
	calls = [["type", "=", "Outgoing"], ["telephony_medium", "in", MEDIA], *([] if run.watermark else [["start_time", ">=", since]])]
	changed = frappe.get_all("CRM Call Log", fields=["name", "caller", "start_time"],
	                         filters=[["modified", ">", since], ["modified", "<=", run.started], *calls])
	if stored:
		changed += frappe.get_all("CRM Call Log", fields=["name", "caller", "start_time"], filters=[["name", "in", stored], *calls])
	# A deleted call leaves its row, and its agent's neighbours lose a flag it set; drop the row and recompute around it.
	deleted = facts.deleted_since("CRM Call Log", since, run.started) if run.watermark else []
	if deleted:
		frappe.db.delete(TARGET, {"call_log": ("in", [name for name, _record in deleted])})
		changed += [frappe._dict(name=name, caller=record.caller, start_time=get_datetime(record.start_time))
		            for name, record in deleted if record.start_time]
	changed = sorted({call.name: call for call in changed}.values(), key=lambda call: call.start_time)
	written = 0
	for lo, hi, agents in spans(changed):
		rows = frappe.db.sql(select_sql(), {"lo": lo - REACH, "hi": hi + REACH, "write_lo": lo - REACH / 2, "write_hi": hi + REACH / 2,
		                                    "agents": tuple(agents), "media": MEDIA}, as_dict=True)
		written += run.write(TARGET, rows)
		run.checkpoint()
	return {"changed": len(changed), "written": written, "deleted": len(deleted)}


def spans(changed):
	"""Changed calls grouped into (first start, last start, agents) runs with no gap over an hour and no run over a day."""
	out = []
	for call in changed:
		if out and call.start_time - out[-1][1] <= GAP and call.start_time - out[-1][0] <= SPAN:
			out[-1][1] = call.start_time
			out[-1][2].add(call.caller or "")
		else:
			out.append([call.start_time, call.start_time, {call.caller or ""}])
	return out
