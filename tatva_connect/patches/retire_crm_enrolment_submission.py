"""Retire the legacy shared intake staging doctype `CRM Enrolment Submission`.

The intake system is now 100% per-form: every `CRM Intake Form` gets its OWN runtime submission
DocType (scaffolded by the builder), processed by the wildcard `route_submission`. The shared
staging sink + its `process_submission` code path are gone. Drop the orphan doctype (and its table)
idempotently; nothing in production ever shipped on it.
"""
import frappe

_DOCTYPE = "CRM Enrolment Submission"


def execute():
	# Any Web Form still bound to the retired sink is legacy (none in prod) — remove first so no
	# public route resolves to a dropped doctype.
	for wf in frappe.get_all("Web Form", filters={"doc_type": _DOCTYPE}, pluck="name"):
		frappe.delete_doc("Web Form", wf, force=True, ignore_permissions=True)
	if frappe.db.exists("DocType", _DOCTYPE):
		frappe.delete_doc("DocType", _DOCTYPE, force=True, ignore_permissions=True)
	# force delete removes the DocType row but LEAVES the tab table orphaned — drop it explicitly.
	# Constant identifier, no interpolation of any value. Idempotent (IF EXISTS).
	frappe.db.sql_ddl(f"DROP TABLE IF EXISTS `tab{_DOCTYPE}`")  # sqli-ok: constant doctype name
