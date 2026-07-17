"""Signal delivery + the signal-wake caller.

DELIVERY IS AN INSERT, NOTHING MORE (F1/F5). `deliver_signal` - the clean whitelisted contract an
external AI / mobile / partner API calls - inserts ONE Pending `CRM Workflow Signal` row and NEVER
touches an Instance. It then OPTIMISTICALLY enqueues `resume_for_signal` (enqueue-after-commit,
job_id/deduplicate, `now` inline in tests). But the enqueue is only a latency optimisation: the durable
inbox row is the source of truth, so a lost job
(Redis flush, worker death) costs only latency - the reconciler sweep re-drives the parked Instance from
the row it can see (`wakeups.reconciler_sweep`). An early delivery (before the Instance parks) simply
waits in the inbox and is consumed the moment the interpreter reaches the Wait - the wakeup cannot drop.

`resume_for_signal` claims the matching `Parked` Instance `for_update`, re-checks status (F6), and
re-enters `advance`, which consumes the inbox row via `_consume_signal`. Idempotent: an Instance that is
no longer parked (already resumed, done, failed, or never parked yet) is a no-op.
"""
import frappe

from tatva_connect import automation
from tatva_connect.workflow_engine import ENGINE_SWITCH, interpreter, wakeups

SIGNAL_DT = interpreter.SIGNAL_DT
INSTANCE_DT = interpreter.INSTANCE_DT


@frappe.whitelist()
def deliver_signal(subject_doctype, subject_name, signal_name, correlation=None, payload=None):
	"""Deliver one signal: insert a Pending inbox row, then optimistically enqueue a resume. Returns the
	inbox row name. Dormant-by-default: with the engine switch off, nothing is delivered (nothing runs)."""
	if not automation.is_enabled(ENGINE_SWITCH):
		return None
	if isinstance(payload, str):
		payload = frappe.parse_json(payload) if payload else None
	row = frappe.get_doc({
		"doctype": SIGNAL_DT,
		"subject_doctype": subject_doctype,
		"subject_name": subject_name,
		"signal_name": signal_name,
		"correlation": correlation,
		"payload_json": frappe.as_json(payload or {}),
		"status": "Pending",
	}).insert(ignore_permissions=True)  # authz-ok: tier-a — workflow engine, external signal ingress
	frappe.enqueue(
		"tatva_connect.workflow_engine.signals.resume_for_signal",
		queue="short",
		enqueue_after_commit=True,
		now=bool(frappe.flags.get("in_test")),
		job_id=f"workflow-signal::{subject_doctype}::{subject_name}::{signal_name}::{correlation or ''}",
		deduplicate=True,
		subject_doctype=subject_doctype,
		subject_name=subject_name,
		signal_name=signal_name,
		correlation=correlation,
	)
	return row.name


def resume_for_signal(subject_doctype, subject_name, signal_name, correlation=None):
	"""Wake the `Parked` Instance waiting on (subject, signal, correlation): claim it `for_update`, re-check
	status, `advance` (which consumes the inbox row). Correlation is matched (null → wildcard, the same trick
	`_consume_signal` uses), so if two journeys on the same subject park on the same signal with different
	correlations the RIGHT one is woken, not an arbitrary one. Idempotent - no matching parked Instance is a
	clean no-op (the inbox row waits until one parks; the reconciler is the backstop)."""
	if not automation.is_enabled(ENGINE_SWITCH):
		return
	filters = {"subject_doctype": subject_doctype, "subject_name": subject_name, "awaiting_signal": signal_name, "status": "Parked"}
	filters["awaiting_correlation"] = correlation if correlation else ["in", ["", None]]
	name = frappe.db.get_value(INSTANCE_DT, filters, "name", for_update=True)
	if not name:
		return  # no Instance parked on this (signal, correlation) yet - waits in the inbox (early-signal safe, F1)
	wakeups.drive_instance(name)
