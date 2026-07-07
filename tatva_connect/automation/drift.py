"""Migrate-time drift check — makes the seam mandatory forever.

Every dotted path wired in `hooks.doc_events` + `hooks.scheduler_events` MUST be
covered by a registry `backs` entry. A handler added to hooks without a registry row
fails `bench migrate` here, so step 3 of the forward rule can never be skipped.

Walks ONLY doc_events + scheduler_events — override_whitelisted_methods,
override_doctype_class, permission_query_conditions, has_permission, after_migrate
and after_request are NOT automations and are intentionally excluded.
"""
import frappe

from tatva_connect import hooks
from tatva_connect.automation.registry import AUTOMATIONS


def _registered_paths():
	paths = set()
	for auto in AUTOMATIONS:
		paths.update(auto.backs)
	return paths


def _hooked_paths():
	"""Flatten every dotted-path string in doc_events + scheduler_events."""
	paths = set()

	# doc_events: doctype -> event -> str | list[str]
	for events in getattr(hooks, "doc_events", {}).values():
		for handlers in events.values():
			if isinstance(handlers, str):
				paths.add(handlers)
			else:
				paths.update(handlers)

	# scheduler_events: bucket (cron/all/daily/...) -> list[str], or cron -> {expr: list}
	for bucket in getattr(hooks, "scheduler_events", {}).values():
		if isinstance(bucket, dict):
			for handlers in bucket.values():
				paths.update(handlers)
		else:
			paths.update(bucket)

	return paths


def assert_registered():
	"""Migrate-time drift check - makes the seam mandatory forever.

	Two checks:
	1. Every dotted path wired in hooks.doc_events + scheduler_events MUST be covered by a
	   registry `backs` entry (a handler added to hooks without a registry row fails migrate).
	2. Every SUBJECT doctype (automation.subjects.SUBJECTS) MUST have a
	   hooks.doc_events[doctype]["on_update"] entry containing
	   `tatva_connect.automation.watch.fire_field_change_rules` - so a watchable doctype can never
	   be left without its dispatch hook (a rule would silently never fire).

	Walks ONLY doc_events + scheduler_events - override_whitelisted_methods,
	override_doctype_class, permission_query_conditions, has_permission, after_migrate
	and after_request are NOT automations and are intentionally excluded.
	"""
	registered = _registered_paths()
	for path in sorted(_hooked_paths()):
		if path not in registered:
			frappe.throw(
				f"Automation drift: '{path}' is wired in hooks.py but has no "
				f"CRM Tatva Automation registry row. Add a `backs` entry in "
				f"tatva_connect/automation/registry.py."
			)
	_assert_subjects_hooked()


def _assert_subjects_hooked():
	"""Every SUBJECT doctype MUST be wired with the Field-Changed dispatch hook in hooks.py - else a
	rule watching it would silently never fire. SUBJECTS (automation.subjects) is the source of truth
	for which doctypes participate; a can_watch allowlist row must name a subject (its own validate),
	so gating on the map, not on table state, is both tighter and DB-independent."""
	from tatva_connect.automation import subjects

	for dt in subjects.subject_doctypes():
		handlers = hooks.doc_events.get(dt, {}).get("on_update", [])
		if isinstance(handlers, str):
			handlers = [handlers]
		if "tatva_connect.automation.watch.fire_field_change_rules" not in handlers:
			frappe.throw(
				f"Automation drift: subject doctype '{dt}' has no on_update hook for "
				f"tatva_connect.automation.watch.fire_field_change_rules in hooks.py. Add it to "
				f"doc_events[\"{dt}\"][\"on_update\"] or remove it from automation.subjects.SUBJECTS."
			)
