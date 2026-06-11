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
	recreate_whatsapp_message_id_index_composite,
)

_STEPS = (
	recreate_whatsapp_message_id_index_composite,
	add_acefone_telephony_medium,
)


def apply_schema():
	for mod in _STEPS:
		try:
			mod.execute()
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(frappe.get_traceback(), f"apply_schema: {mod.__name__}")
