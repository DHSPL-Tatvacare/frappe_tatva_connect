# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Authorization gates over native crm whitelisted methods that BYPASS the permission engine
(get_all / ignore_permissions / un-gated get_doc) — so neither the doctype matrix NOR a
doctype's has_permission hook can reach them.

Each wrapper asserts the caller may act on the target, then delegates to the UNCHANGED native
function. Wired via override_whitelisted_methods (hooks.py): the crm endpoint runs our gate
first; the native code runs verbatim once the gate passes. No crm fork.

The native function is imported DIRECTLY (not dispatched), so it never re-enters the override —
no recursion. Contract (same as the brain, §5): a read leak is gated on READ of the referenced
doc; a write/create is gated on the doctype's write/create. Row-scope is a separate layer.
"""
import frappe


def _require_read(doctype, name):
	frappe.has_permission(doctype, "read", name, throw=True)


# --- Telephony / Call Log (call_sid / call_log_name == CRM Call Log name) ----------------------
@frappe.whitelist()
def add_task_to_call_log(call_sid, task):
	_require_read("CRM Call Log", call_sid)
	from crm.integrations.api import add_task_to_call_log as _native

	return _native(call_sid, task)


@frappe.whitelist()
def add_note_to_call_log(call_sid, note):
	_require_read("CRM Call Log", call_sid)
	from crm.integrations.api import add_note_to_call_log as _native

	return _native(call_sid, note)


@frappe.whitelist()
def get_recording_url(call_log_name):
	_require_read("CRM Call Log", call_log_name)
	from crm.integrations.api import get_recording_url as _native

	return _native(call_log_name)


@frappe.whitelist()
def set_default_calling_medium(medium):
	frappe.has_permission("CRM Telephony Agent", "create", throw=True)
	from crm.integrations.api import set_default_calling_medium as _native

	return _native(medium)


# --- Generic doc (doctype + name supplied by caller) -------------------------------------------
@frappe.whitelist()
def get_assigned_users(doctype, name, default_assigned_to=None):
	_require_read(doctype, name)
	from crm.api.doc import get_assigned_users as _native

	return _native(doctype, name, default_assigned_to)


@frappe.whitelist()
def get_linked_docs_of_document(doctype, docname):
	_require_read(doctype, docname)
	from crm.api.doc import get_linked_docs_of_document as _native

	return _native(doctype, docname)


# --- Deal ---------------------------------------------------------------------------------------
@frappe.whitelist()
def create_deal(doc):
	frappe.has_permission("CRM Deal", "create", throw=True)
	from crm.fcrm.doctype.crm_deal.crm_deal import create_deal as _native

	return _native(doc)


@frappe.whitelist()
def get_deal_contacts(name):
	_require_read("CRM Deal", name)
	from crm.fcrm.doctype.crm_deal.api import get_deal_contacts as _native

	return _native(name)


# NOTE: crm's deal-contact MUTATORS (add_contact / remove_contact / set_primary_contact) already
# gate on `has_permission("CRM Deal", "write")` themselves, so they are NOT wrapped here — adding a
# second identical gate would be a redundant parallel path. Only engine-bypassing methods are wrapped.


# --- Contact lookup (no specific doc -> doctype-level READ gate) --------------------------------
@frappe.whitelist()
def get_contact_by_phone_number(phone_number):
	frappe.has_permission("Contact", "read", throw=True)
	from crm.integrations.api import get_contact_by_phone_number as _native

	return _native(phone_number)


@frappe.whitelist()
def get_contact_lead_or_deal_from_number(number):
	frappe.has_permission("Contact", "read", throw=True)
	from crm.integrations.api import get_contact_lead_or_deal_from_number as _native

	return _native(number)


# --- WhatsApp ----------------------------------------------------------------------------------
@frappe.whitelist()
def get_whatsapp_messages(reference_doctype, reference_name):
	_require_read(reference_doctype, reference_name)
	from crm.api.whatsapp import get_whatsapp_messages as _native

	return _native(reference_doctype, reference_name)
