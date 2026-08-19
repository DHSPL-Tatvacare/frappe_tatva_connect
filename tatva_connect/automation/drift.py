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
	2. The wildcard automation router (`router.on_created`/`on_updated`/`on_deleted`) MUST be wired
	   on `hooks.doc_events["*"]` for after_insert/on_update/on_trash - see `_assert_router_wired`.

	Walks ONLY doc_events + scheduler_events - override_whitelisted_methods,
	override_doctype_class, permission_query_conditions, has_permission, after_migrate,
	after_request and after_job are NOT automations and are intentionally excluded.
	"""
	registered = _registered_paths()
	for path in sorted(_hooked_paths()):
		if path not in registered:
			frappe.throw(
				f"Automation drift: '{path}' is wired in hooks.py but has no "
				f"CRM Tatva Automation registry row. Add a `backs` entry in "
				f"tatva_connect/automation/registry.py."
			)
	_assert_engine_wired()


def _assert_engine_wired():
	"""The unified Flow engine has no per-doctype hook - EVERY grain-resolvable subject is covered by the
	wildcard front-door (`triggers.on_created`/`on_updated`/`on_trash` on `doc_events["*"]`), and a
	doctype only acts because an ENABLED Flow names it. So the one thing that can silently break
	automation for EVERY doctype at once is the wildcard registration itself - assert it directly.

	`on_created` rides `on_update`, NOT `after_insert`, and that is asserted in both directions. Its own
	`in_insert` guard is what keeps "Created" meaning created; the event it listens on is chosen so that
	frappe's Assignment Rule - which runs on `on_update` alone - has already picked the owner first."""
	created = hooks.doc_events.get("*", {}).get("after_insert", [])
	updated = hooks.doc_events.get("*", {}).get("on_update", [])
	deleted = hooks.doc_events.get("*", {}).get("on_trash", [])
	created = [created] if isinstance(created, str) else created
	updated = [updated] if isinstance(updated, str) else updated
	deleted = [deleted] if isinstance(deleted, str) else deleted
	if "tatva_connect.workflow_engine.triggers.on_created" not in updated:
		frappe.throw('Automation drift: the Flow engine\'s on_created is not wired in hooks.doc_events["*"]["on_update"].')
	# The event it must NOT be on. `on_created` lived here until a Created flow was found raising tasks
	# before the lead had an owner: frappe registers `assignment_rule.apply` on `on_update` ONLY
	# (frappe/hooks.py:155-166) and `after_insert` runs first, so the flow could never see the assignment.
	# Asserting the absence is what stops a future edit quietly moving it back and re-breaking the order.
	if "tatva_connect.workflow_engine.triggers.on_created" in created:
		frappe.throw('Automation drift: the Flow engine\'s on_created is wired in hooks.doc_events["*"]["after_insert"]; it belongs on ["on_update"], which is the only event frappe\'s Assignment Rule runs on, so a Created flow acts on a lead that already has an owner.')
	if "tatva_connect.workflow_engine.triggers.on_updated" not in updated:
		frappe.throw('Automation drift: the Flow engine\'s on_updated is not wired in hooks.doc_events["*"]["on_update"].')
	if "tatva_connect.workflow_engine.triggers.on_trash" not in deleted:
		frappe.throw('Automation drift: the Flow engine\'s on_trash is not wired in hooks.doc_events["*"]["on_trash"].')
