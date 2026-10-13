"""Which notification channels the work in hand must not use: a Live bulk import's ticks, carried to the journeys it starts."""
import contextlib

import frappe

from tatva_connect.automation.settings import running_bulk_job

EMAIL, IN_APP, PUSH = "email", "in_app", "push"

# Each channel's tick, the same fieldname on CRM Lead Import and CRM Bulk Job.
SKIP_FIELD = {EMAIL: "skip_email", IN_APP: "skip_in_app", PUSH: "skip_push"}

# The bulk job of the journey `interpreter.advance` is walking; absent outside one.
_JOURNEY_JOB = "tatva_journey_bulk_job"


def source_job():
	"""The CRM Bulk Job the work in hand came from: the one this process runs, else the journey's being walked."""
	return running_bulk_job() or getattr(frappe.local, _JOURNEY_JOB, None)


def muted(channel):
	"""True when the work in hand came from a bulk job that ticked `channel`; a cached read, so no query per send."""
	name = source_job()
	return bool(name) and bool(frappe.get_cached_value("CRM Bulk Job", name, SKIP_FIELD[channel]))


@contextlib.contextmanager
def journey(bulk_job):
	"""Hold a journey's source bulk job for the length of one walk, so every step asks `muted` of it."""
	held = getattr(frappe.local, _JOURNEY_JOB, None)
	setattr(frappe.local, _JOURNEY_JOB, bulk_job)
	try:
		yield
	finally:
		setattr(frappe.local, _JOURNEY_JOB, held)  # restored, not deleted, so a walk inside a walk leaves the outer one intact
