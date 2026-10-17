"""The signal inbox: what is admitted, what wakes a parked journey, and the re-drive when a wake was lost.

`deliver_signal` stores one Pending `CRM Workflow Signal` row and pulls the workflow drain's next pass forward; the
row is the truth and the pass is what acts on it (F1/F5). A storm of receipts therefore moves ONE booking rather than
queueing a job each, so every wake is paced, ordered and held back by a busy lane like all the drain's other work. An
early signal waits until its Wait is reached, and `admits` refuses a row no journey could ever consume.
"""
import time

import frappe
from frappe import _

from tatva_connect import automation
from tatva_connect.workflow_engine import ENGINE_SWITCH, interpreter, thresholds, wakeups

SIGNAL_DT = interpreter.SIGNAL_DT
JOURNEY_DT = interpreter.JOURNEY_DT


@frappe.whitelist(methods=["POST"])
def deliver_signal(subject_doctype, subject_name, signal_name, correlation=None, payload=None):
	"""Deliver one signal: insert a Pending inbox row and pull the drain's next pass forward. Returns the
	inbox row name. Dormant-by-default: with the engine switch off, nothing is delivered (nothing runs).

	PERMISSION-GATED, because this is a whitelisted write into a running workflow. The payload it carries
	is merged into journey state by the Wait's `accepts` map, where it feeds Route predicates and every
	effect verb — so an ungated caller could advance another team's journey on a lead they cannot see and
	choose the values that drive its sends. The gate is write on the SUBJECT: if you may not write the
	record, you may not move a workflow that is watching it."""
	if not frappe.db.exists("DocType", subject_doctype):
		frappe.throw(_("Unknown doctype {0}").format(subject_doctype))
	if not frappe.has_permission(subject_doctype, "write", doc=subject_name):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	if not automation.is_enabled(ENGINE_SWITCH):
		return None
	if not admits(signal_name, correlation):
		return None
	if isinstance(payload, str):
		payload = frappe.parse_json(payload) if payload else None
	row = frappe.get_doc({
		"doctype": SIGNAL_DT,
		"subject_doctype": subject_doctype,
		"subject_name": subject_name,
		"event_name": signal_name,
		"correlation": correlation,
		"payload_json": frappe.as_json(payload or {}),
		"status": interpreter.PENDING,
	}).insert(ignore_permissions=True)  # authz-ok: tier-a — workflow engine, external signal ingress
	from tatva_connect.workflow_engine import drain

	# One booking, however many arrive at once: a storm replaces it rather than stacking a job each.
	drain.pull_forward(frappe.utils.now_datetime())
	return row.name


def resume_for_signal(subject_doctype, subject_name, signal_name, correlation=None):
	"""Wake the `Parked` Journey waiting on (subject, signal, correlation): claim it `for_update`, re-check
	status, `advance` (which consumes the inbox row). Correlation is matched (null → wildcard, the same trick
	`_consume_signal` uses), so if two journeys on the same subject park on the same signal with different
	correlations the RIGHT one is woken, not an arbitrary one. Idempotent - no matching parked Journey is a
	clean no-op: the row waits in place and the next pass asks again."""
	if not automation.is_enabled(ENGINE_SWITCH):
		return
	# A run another driver holds is theirs: the pass skips it rather than waiting on its lock, as `wake_due` does.
	name = frappe.db.get_value(JOURNEY_DT, parked_filters(subject_doctype, subject_name, signal_name, correlation), "name",
	                           for_update=True, skip_locked=True)
	if not name:
		return  # no Journey parked on this (signal, correlation) yet, or held - waits in the inbox (early-signal safe, F1)
	wakeups.drive_journey(name, skip_locked=True)


def parked_filters(subject_doctype, subject_name, signal_name, correlation=None):
	"""Filters for the `Parked` journey waiting on (subject, signal, correlation) — shared by the wake and the re-drive."""
	filters = {"subject_doctype": subject_doctype, "subject_name": subject_name, "awaiting_signal": signal_name, "status": "Parked"}
	filters["awaiting_correlation"] = interpreter.correlation_filter(correlation)
	return filters


def admits(signal_name, correlation):
	"""Whether any journey could ever consume this signal; a row nothing can take is not stored.

	An engine token (`journey::node`) names the one journey that could take it: refused when that journey is gone,
	finished, or has no Wait on this event from that node. Any other correlation is admitted unchanged.
	"""
	journey, _sep, node_id = (correlation or "").partition("::")
	if not node_id:
		return True
	row = frappe.db.get_value(JOURNEY_DT, journey, ["status", "workflow_version"], as_dict=True)
	if not row or row.status not in interpreter.LIVE_STATES:
		return False
	return interpreter.awaits(interpreter.versions_load(row.workflow_version), signal_name, node_id)


def claimable(limit, after=None):
	"""Pending signals a parked journey is waiting on, oldest first and past the cursor: ONE query, the inbox joined to the runs that can take it.

	The correlation is matched by `interpreter.correlation_join`, the claim's own rule in its column form.
	"""
	signal, journey = frappe.qb.DocType(SIGNAL_DT), frappe.qb.DocType(JOURNEY_DT)
	query = (
		frappe.qb.from_(signal)
		.join(journey)
		.on((journey.subject_doctype == signal.subject_doctype) & (journey.subject_name == signal.subject_name)
		    & (journey.awaiting_signal == signal.event_name) & (journey.status == "Parked")
		    & interpreter.correlation_join(journey.awaiting_correlation, signal.correlation))
		.select(signal.name, signal.creation, signal.subject_doctype, signal.subject_name, signal.event_name,
		        signal.correlation)
		.distinct()
		.where(signal.status == interpreter.PENDING)
		.orderby(signal.creation)
		.orderby(signal.name)
		.limit(limit)
	)
	if after:
		query = query.where(
			(signal.creation > after.creation) | ((signal.creation == after.creation) & (signal.name > after.name))
		)
	return query.run(as_dict=True)


def redrive(limit, until, renew):
	"""Wake up to `limit` journeys whose signals are waiting in the inbox — the drain's half of every delivery (F1/F5).

	Pages only the signals a parked run can take, oldest first, so none starves behind the same page. Each wake goes
	through `resume_for_signal`, the one claim. Returns how many it woke.
	"""
	driven, after = 0, None
	while driven < limit and time.monotonic() < until:
		rows = claimable(min(thresholds.SWEEP_PAGE, limit - driven), after)
		if not rows:
			break
		for row in rows:
			if time.monotonic() >= until:
				break
			renew()
			# Each wake reads its own snapshot, as every claim in the drain does.
			frappe.db.commit()
			resume_for_signal(row.subject_doctype, row.subject_name, row.event_name, row.correlation)
			frappe.db.commit()
			driven += 1
		after = rows[-1]
	return driven
