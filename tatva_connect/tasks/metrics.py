"""Activity-metrics recompute engine — per-lead task counts kept correct, OFF by default.

One brain for the per-lead activity rollup stored on the `CRM Lead Activity Metrics`
child row (one row per lead, via the lead's `custom_lead_activity_metrics` Table field).

How it stays correct & safe:
  * RECOMPUTE, never increment — every fire writes the ABSOLUTE count
    (`frappe.db.count`), so it's self-healing (a missed/duplicated event can't drift
    the value) and there is no `+1` race.
  * INJECTION-SAFE — the metric column we write is resolved ONLY from the code-owned
    `TASK_TYPE_TO_METRIC` dict, NEVER from the task/DB/request. A crafted
    `custom_task_type` that isn't in the dict resolves to nothing and writes nothing
    (no column-name injection). The count's filters are parameterised by Frappe.
  * PERMLEVEL — the count fields are permlevel 1 (no normal role writes them on the
    lead form; Frappe resets them to the DB value on save). The engine writes them via
    `frappe.db.set_value` (system path, bypasses permlevel), so background counts are
    machine-owned and the form can never clobber them.
  * GATED — every entrypoint returns immediately unless the operator switch
    `Activity::Metrics::rollup` is ON (registered in automation/registry.py; seeded
    OFF on after_migrate; drift-checker enforced). Code ships dormant.
"""
import frappe

from tatva_connect import automation

_GATE = "Activity::Metrics::rollup"
_METRICS_DT = "CRM Lead Activity Metrics"
_PARENT_DT = "CRM Lead"
_PARENTFIELD = "custom_lead_activity_metrics"
_DONE_STATUS = "Done"

# CRM Task `custom_task_type` -> the `CRM Lead Activity Metrics` count fieldname it feeds.
#
# BINDING POINT: the keys are the exact `custom_task_type` strings, which come from the
# ACTIVITY CATALOG (CRM Task Type rows) — they are OPERATOR/business data, NOT code. They are
# reconciled here against the authoritative LSQ migration map (docs/migration/tatvapractice/
# mapping.json → "activity_task_types") and verified against the live catalog (distinct
# `custom_task_type` on CRM Task). The VALUES are the only column names this engine will ever
# write — they are code-owned and every one MUST exist as an Int count field on
# `CRM Lead Activity Metrics`. This engine feeds ONLY the "completed-of-type" counters: a
# count of `status=Done` tasks for each (lead, task_type). Each is one visit-mode of one
# activity, so exactly the 12 (6 field-visit + 6 phone-call) types below map here.
#
# DELIBERATELY UNMAPPED — fed elsewhere, NOT a Done-count-by-task-type, so out of scope:
#   * Total rollups (custom_total_visit_count, custom_total_phone_call_count,
#     custom_total_visits) — sums across types, not a single type's count.
#   * Reschedule counters (custom_*_reschedule_count, custom_*_rescheduled_count,
#     custom_intro_visit_reschedule_count, custom_activation_reschedule_count,
#     custom_call_reschedule_count, custom_demo_reschedule_count) — driven by an activity's
#     reschedule_date_time field, not by task status.
#   * Outcome counters (custom_not_reachable_count, custom_rnr_count) — driven by an
#     activity's outcome value, and the catalog carries no "Not Reachable"/"RNR" task type.
#   * custom_doctor_visit_not_completed_count_intro — a not-completed counter, not Done.
# All of these arrive as absolute values from the LSQ migration (mapping.json children block)
# and are not recomputed by this Done-count engine.
TASK_TYPE_TO_METRIC = {
	# --- field visits (one visit-mode per activity) ---
	"Demo Scheduled Status Field Visit": "custom_demo_visit_count",
	"Courtesy Visit Field Visit": "custom_courtesy_visit_count",
	"Onboarding Status Field Visit": "custom_onboard_visit_count",
	"Doctor Training Field Visit": "custom_trainings_visit_count",
	"Introductory Meeting Physical Visit": "custom_introductory_meeting_visit_count",
	"Doctor Activation Field Visit": "custom_doctor_activation_visit_count",
	# --- phone calls (the other visit-mode per activity) ---
	"Demo Scheduled Status Phone Call": "custom_demo_call_count",
	"Courtesy Visit Phone Call": "custom_courtesy_phone_call_count",
	"Onboarding Status Phone Call": "custom_onboarding_phone_call_count",
	"Doctor Training Phone Call": "custom_trainings_call_count",
	"Introductory Meeting Phone Call": "custom_introductory_meeting_phone_call_count",
	"Doctor Activation Phone Call": "custom_doctor_activation_phone_call_count",
}


def _metric_field(task_type):
	"""The count column for a task type — ONLY from the code dict (never from the doc).

	Defends the `set_value` fieldname against injection: an unknown/crafted task type
	returns None and the caller writes nothing. The `assert` makes a typo in the dict a
	loud failure rather than a silent bad column name."""
	field = TASK_TYPE_TO_METRIC.get(task_type)
	if field is None:
		return None
	assert field in TASK_TYPE_TO_METRIC.values()  # field name is code-owned, never external
	return field


def _get_or_create_row(lead):
	"""The lead's single metrics child row name — found by (parent, parenttype, parentfield),
	created empty if missing. One row per lead (the parent Table field is single-valued)."""
	row = frappe.db.get_value(
		_METRICS_DT,
		{"parent": lead, "parenttype": _PARENT_DT, "parentfield": _PARENTFIELD},
		"name",
	)
	if row:
		return row
	child = frappe.get_doc(
		{
			"doctype": _METRICS_DT,
			"parent": lead,
			"parenttype": _PARENT_DT,
			"parentfield": _PARENTFIELD,
			"parentdocname": lead,
		}
	)
	child.insert(ignore_permissions=True)
	return child.name


def refresh_for_lead(doc, method=None):
	"""CRM Task on_update/on_submit/on_cancel/on_trash — recompute the ONE count this task's
	type feeds onto its lead's metrics row. Gated OFF by default; absolute recompute; the
	written column comes only from the code dict."""
	if not automation.is_enabled(_GATE):
		return
	# reference_doctype is a free DocType link + reference_docname a Dynamic Link, so a task
	# may point at a non-Lead doc (e.g. CRM Deal); scope strictly to CRM Lead or we'd write a
	# metrics row keyed to a non-lead parent and over-count on any name collision.
	if doc.reference_doctype != _PARENT_DT:
		return
	if doc.reference_docname is None:
		return
	field = _metric_field(doc.custom_task_type)
	if field is None:
		return  # task type not mapped to a metric — nothing to roll up

	n = frappe.db.count(
		"CRM Task",
		{
			"reference_doctype": _PARENT_DT,
			"reference_docname": doc.reference_docname,
			"custom_task_type": doc.custom_task_type,
			"status": _DONE_STATUS,
		},
	)
	# on_trash fires BEFORE the row is removed from the table (Frappe delete_doc: on_trash
	# at delete_doc.py:165, row dropped at delete_from_table:183), so the count above still
	# sees this about-to-be-deleted task. Subtract it so a delete actually decrements.
	if (method == "on_trash" or getattr(doc.flags, "in_delete", False)) and doc.status == _DONE_STATUS:
		n = max(0, n - 1)
	row = _get_or_create_row(doc.reference_docname)
	# set_value = system path (bypasses the permlevel-1 lock); update_modified=False so a
	# background count never bumps the lead's modified timestamp.
	frappe.db.set_value(_METRICS_DT, row, field, n, update_modified=False)


def reconcile_all():
	"""Scheduler-safe full recompute across all leads (GROUP BY reference_docname,
	custom_task_type). Same gate; idempotent absolute writes. Optional maintenance sweep."""
	if not automation.is_enabled(_GATE):
		return
	_aggregate_write()


def backfill():
	"""One-time aggregate seed of the counts after a data load (GROUP BY, NOT a per-lead loop).
	Operator-run; same absolute-write brain as reconcile_all."""
	_aggregate_write()


def _aggregate_write():
	"""Compute every (lead, task_type) Done count in ONE grouped pass and write each onto its
	lead's metrics row. Only task types present in the code dict are written (injection-safe)."""
	rows = frappe.get_all(
		"CRM Task",
		filters={
			"status": _DONE_STATUS,
			"reference_doctype": _PARENT_DT,
			"reference_docname": ["is", "set"],
			"custom_task_type": ["in", list(TASK_TYPE_TO_METRIC.keys())],
		},
		fields=["reference_docname as lead", "custom_task_type as task_type", "count(name) as n"],
		group_by="reference_docname, custom_task_type",
	)
	for r in rows:
		field = _metric_field(r.task_type)
		if field is None:
			continue
		metrics_row = _get_or_create_row(r.lead)
		frappe.db.set_value(_METRICS_DT, metrics_row, field, r.n, update_modified=False)
