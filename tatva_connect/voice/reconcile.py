"""Catch-up poll for voice calls whose terminal webhook never arrived. DORMANT — nothing calls this.

WHY IT EXISTS. The webhook is the fast path and it is not a guarantee: a provider can drop a callback, a
deploy can eat one, and the run parked behind it then waits for an event that will never come. The evals
platform learned this and answered it with a poller, and this is that poller ported to our shapes —
correctness never depends on a callback arriving.

WHY IT IS OFF. It is gated on `AI Voice::Channel::reconcile`, a switch whose row does not exist, so
`settings.is_enabled` reads False and every entry point below returns immediately. Two lines still have to
be written by hand before it can ever run, and they are deliberately NOT written here (both files are
being edited by another change):

    hooks.py            scheduler_events["cron"]["*/15 * * * *"] += ["tatva_connect.voice.reconcile.sweep"]
    automation/registry.py   Auto("AI Voice::Channel::reconcile", "AI Voice — catch up on call outcomes", ...)

Until both exist this module is a function nothing schedules and no operator can enable.

WHAT IT DOES NOT DO. It does not classify — `bolna.classify_outcome` is the one classifier and this asks
it. It does not wake runs by a second route — it builds the provider's own execution payload and hands it
to `bolna.handle`, the same function the webhook worker calls, so a reconciled outcome and a delivered one
are the same event by construction. It never places a call.
"""
import re

import frappe
from frappe.utils import add_to_date, now_datetime

from tatva_connect.voice import channel

# How long a call is given to report itself before the poll goes looking. Comfortably past any real call.
GRACE_MINUTES = 30

# One sweep's bound. A backlog is drained over several passes rather than in one long job.
BATCH = 50

_DETAIL_PREFIX = "execution_id="


def sweep():
	"""Poll every run parked on a voice outcome past the grace window. The scheduler entry point.

	Dormant: with the switch off this is the whole function. Nothing is read, nothing is polled.
	"""
	if not channel.reconciler_enabled():
		return 0
	reconciled = 0
	for run in _stale_parked_runs():
		if reconcile_run(run.name, run.awaiting_correlation):
			reconciled += 1
	return reconciled


def _stale_parked_runs():
	"""Runs parked on a voice event, untouched since the grace window closed.

	`awaiting_signal` is the flavour column `_park` writes, so this finds exactly the runs a voice callback
	would have woken — never a run parked on a timer or on some other channel's event.
	"""
	return frappe.get_all(  # authz-ok: tier-a — workflow engine, scheduler context
		"CRM Workflow Run",
		filters={
			"status": "Parked",
			"awaiting_signal": ["like", "voice.%"],
			"awaiting_correlation": ["is", "set"],
			"modified": ["<", add_to_date(now_datetime(), minutes=-GRACE_MINUTES)],
		},
		fields=["name", "awaiting_correlation"],
		limit=BATCH,
		order_by="modified asc",
	)


def reconcile_run(run, correlation):
	"""Ask the provider what became of this run's call, and deliver the outcome if it is terminal.

	Returns True when an outcome was delivered. A call still in flight, an execution we cannot identify,
	or a provider that will not answer all leave the run exactly as it was — a poll that cannot tell must
	never invent an outcome, because the outcome it invents routes a real patient down a real branch.
	"""
	if not channel.reconciler_enabled():
		return False
	execution_id, account = _placement_of(run, correlation)
	if not (execution_id and account):
		return False

	from tatva_connect.voice import api as voice_api

	adapter = voice_api.adapter_for(account)
	try:
		execution = adapter.fetch_execution(voice_api.connection_for(account), execution_id)
	except Exception:
		frappe.log_error(title="voice reconcile: execution fetch failed",
		                 message=f"run={run} execution_id={execution_id}\n{frappe.get_traceback()}")
		return False

	if not adapter.is_terminal(execution.get("status")):
		return False

	# The provider's payload carries no `user_data` on this route, so the token this run is parked on is
	# put back where the callback would have echoed it. Same shape in, same `handle`, same wake.
	execution = {**execution, "user_data": {adapter.USER_DATA_CORRELATION_KEY: correlation}}
	if adapter.already_processed(execution, None, account):
		return False
	adapter.handle(execution, None, account)
	return True


def _placement_of(run, correlation):
	"""(execution_id, account) for the node this run is parked behind, from the placement audit row.

	`_log_voice_placement` writes exactly one row per placed call, carrying both. Reading it back is what
	lets the poll exist without a call-record table: the run knows its token, the token names the node, and
	the node's own step log knows which provider execution it became.
	"""
	_run, _, node_id = (correlation or "").partition("::")
	if not node_id:
		return None, None
	rows = frappe.get_all(  # authz-ok: tier-a — workflow engine, scheduler context
		"CRM Workflow Step Log",
		filters={"workflow_run": run, "node_id": node_id, "detail": ["like", f"{_DETAIL_PREFIX}%"]},
		fields=["detail"],
		limit=1,
		order_by="creation desc",
	)
	return _parse_detail(rows[0].detail) if rows else (None, None)


def _parse_detail(detail):
	"""`execution_id=<id> account=<name>` -> the pair. The writer and this reader are the one format.

	The account is taken to the end of the line rather than to the next space: an account name is an
	operator's display name and may contain them. The execution id never does.
	"""
	match = re.match(rf"^{_DETAIL_PREFIX}(\S+)\s+account=(.*)$", (detail or "").strip())
	if not match:
		return None, None
	return match.group(1) or None, match.group(2).strip() or None
