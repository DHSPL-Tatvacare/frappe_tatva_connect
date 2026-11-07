"""Every analytics job, one module each: JOB, TARGET, WINDOW_DAYS, optional AFTER, select_sql() and refresh(run) -> counts.

A new fact or summary is one module here, its line in MODELS, and its target DocType; migrate creates its Control row, off.
"""
from tatva_connect.tatva_analytics.models import (
	call_fact,
	call_summary,
	lead_allocation_fact,
	pool_backlog_summary,
	shift_activity_fact,
)

MODELS = (call_fact, call_summary, lead_allocation_fact, pool_backlog_summary, shift_activity_fact)


def by_job():
	return {model.JOB: model for model in MODELS}
