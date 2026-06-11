"""Mirror WATI templates into the read-only `WhatsApp Templates` catalog.

The CRM picker reads the `WhatsApp Templates` doctype. WATI templates already
exist and are approved on WATI; we never create or edit them from Frappe. This
pulls `getMessageTemplates` and writes local rows as a **read-only reflection**,
using `db_insert` / `db_update` to bypass frappe_whatsapp's Meta-bound
validate/after_insert entirely. No Meta, no WATI push.
"""
import json

import frappe

from tatva_connect.whatsapp import api as wati


@frappe.whitelist()
def sync_from_wati(account_name=None):
	"""Reflect approved WATI templates into WhatsApp Templates. Returns per-account counts.

	With no `account_name`, syncs EVERY WATI account (each tenant has its own
	template namespace). Records are keyed per account so two tenants can share a
	template name without clobbering each other.
	"""
	if account_name:
		accounts = [account_name]
	else:
		accounts = frappe.get_all("WhatsApp Account", filters={"custom_is_wati": 1}, pluck="name")
	if not accounts:
		frappe.throw("No WATI WhatsApp Account found to sync templates from.")

	totals = {}
	for acc in accounts:
		totals[acc] = _sync_one(acc)
	frappe.db.commit()
	return totals


def scheduled_sync_all():
	"""Scheduler hook (every 6h): refresh all WATI accounts' templates.

	A good-have backstop on top of the real-time manual sync. Respects the
	kill-switch and never lets a sync failure raise out of the scheduler.
	"""
	if not wati.is_enabled():
		return
	try:
		totals = sync_from_wati()
		frappe.logger("tatva_connect").info(f"WATI scheduled template sync: {totals}")
	except Exception:
		frappe.log_error(
			title="WATI scheduled template sync failed",
			message=frappe.get_traceback(),
		)


def _record_name(element_name, account_name):
	"""Account-scoped record id so the same template name can exist per tenant."""
	return f"{element_name}::{account_name}"


def _sync_one(account_name):
	account = frappe.get_doc("WhatsApp Account", account_name)
	wati.assert_wati(account)

	resp = wati.get_message_templates(account)
	items = resp.get("messageTemplates") or resp.get("templates") or resp.get("data") or []

	created = updated = skipped = 0
	for t in items:
		try:
			if (t.get("status") or "").upper() != "APPROVED":
				continue
			element_name = t.get("elementName")
			if not element_name:
				continue
			# Sample values, keyed by param name ({"1": "...", "2": "..."}), so the
			# picker can show an exact per-variable hint. The {{N}} -> CRM field
			# mapping lives in `field_names` (operator-set) — never overwritten here.
			custom_params = t.get("customParams") or []
			sample_values = json.dumps(
				{
					str(p.get("paramName") or i + 1): (p.get("paramValue") or "")
					for i, p in enumerate(custom_params)
				}
			)
			lang = t.get("language")
			lang_code = (lang.get("value") if isinstance(lang, dict) else lang) or "en"
			record_name = _record_name(element_name, account_name)
			values = {
				# template_name (unique) carries the account-scoped id so two
				# tenants with the same template name don't collide on one row.
				"template_name": record_name,
				"actual_name": element_name,  # the real WATI name used to send
				"language_code": str(lang_code).replace("-", "_"),
				"status": "APPROVED",
				"category": t.get("category") or "UTILITY",
				"template": t.get("body") or "",
				"sample_values": sample_values,
				"whatsapp_account": account_name,
			}
			if frappe.db.exists("WhatsApp Templates", record_name):
				doc = frappe.get_doc("WhatsApp Templates", record_name)
				doc.update(values)
				doc.db_update()  # bypass Meta-bound validate/update_template
				updated += 1
			else:
				doc = frappe.new_doc("WhatsApp Templates")
				doc.update(values)
				doc.name = record_name
				doc.db_insert()  # bypass Meta-bound after_insert
				created += 1
		except Exception:
			# One malformed template must not abort the whole batch.
			skipped += 1
			frappe.log_error(
				title="WATI template sync skipped one",
				message=f"account={account_name} template={t.get('elementName')}\n{frappe.get_traceback()}",
			)

	return {"created": created, "updated": updated, "skipped": skipped}
