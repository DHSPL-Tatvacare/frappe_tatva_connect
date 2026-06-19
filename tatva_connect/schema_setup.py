"""Apply idempotent SCHEMA patches on after_migrate.

`install-app` BASELINES patches.txt without running it (see seeds.py), so structural
changes done via patches — indexes, Select options, custom fields — never land on a
fresh DB. These are idempotent, so we also run them on after_migrate: a no-op on an
existing DB that already has them, and the thing that actually builds them on a clean
install. They stay in patches.txt too (existing-DB ordering + history).

Only TWO things genuinely belong here — structures we can't put in a file:
  * the composite WhatsApp index — the `WhatsApp Message` doctype belongs to the upstream
    frappe_whatsapp app (no-fork rule), so we can't declare the index in its JSON.
  * the Acefone telephony medium — appends "Acefone" to a Select on frappe/crm's own
    doctypes; a fixture would REPLACE their options (and drift across crm versions), so we
    merge in code instead of clobbering.
(The Azure File marker is a plain custom field, so it ships as a fixture, not here.)
Each step isolates its own failure (rollback + log) so one gap never aborts the migrate.
"""
import frappe

from tatva_connect.patches import (
	add_acefone_telephony_medium,
	add_observability_indexes,
	migrate_webhook_tokens_to_password,
	recreate_whatsapp_message_id_index_composite,
	retire_lead_stage_legacy_fields,
	retire_location_captures_fields,
)

_STEPS = (
	recreate_whatsapp_message_id_index_composite,
	add_acefone_telephony_medium,
	retire_location_captures_fields,
	retire_lead_stage_legacy_fields,
	add_observability_indexes,
	# Carry existing webhook tokens into the Password store after the Data->Password flip.
	# install-app baselines patches.txt without running it, so this is the path that lands the
	# carry on an existing DB's first redeploy; a fresh install has blank tokens (clean no-op).
	migrate_webhook_tokens_to_password,
)


def apply_schema():
	for mod in _STEPS:
		try:
			mod.execute()
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(frappe.get_traceback(), f"apply_schema: {mod.__name__}")
	_ensure_fixed_settings()
	_ensure_field_map_role()
	_assert_observability_bands()


def _assert_observability_bands():
	"""Fail loudly if the latency-band single-source-of-truth drifts from the CRM API Metric
	columns (a new band added to LATENCY_BANDS without a matching column, or vice versa)."""
	try:
		from tatva_connect.observability.constants import assert_bands_match_schema

		assert_bands_match_schema()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "apply_schema: observability bands")


def _ensure_field_map_role():
	"""Seed the `Field Map User` role — the single gate for the Near Me directory page in the CRM
	fork. The role's EXISTENCE is structural to the feature (identical in every deployment), so it
	belongs here; ASSIGNING it to users is operator business data and is NEVER done in code. The
	page also requires the `Location::NearMe::directory` switch ON, so a seeded-but-unassigned role
	plus an off switch keeps the feature fully dormant (CLAUDE.md #4/#6). Idempotent; isolated."""
	try:
		if not frappe.db.exists("Role", "Field Map User"):
			role = frappe.new_doc("Role")
			role.role_name = "Field Map User"
			role.desk_access = 0  # SPA-only role; no Desk surface
			role.save(ignore_permissions=True)
			frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "apply_schema: ensure_field_map_role")


def _ensure_fixed_settings():
	"""Backfill display-only fixed values on single settings (a field default never reaches an existing
	single doc). geocoding_provider is always Google — shown grayed in CRM Maps Settings for reference.
	Idempotent; isolates its own failure so it never aborts the migrate."""
	try:
		if frappe.db.exists("DocType", "CRM Maps Settings"):
			frappe.db.set_single_value("CRM Maps Settings", "geocoding_provider", "Google")
			frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "apply_schema: ensure_fixed_settings")
