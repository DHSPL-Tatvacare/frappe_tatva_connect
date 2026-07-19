"""Mirror provider templates into the read-only `WhatsApp Templates` catalog.

The CRM picker reads the `WhatsApp Templates` doctype. The templates already exist and
are approved on the provider; we never create or edit them from Frappe. This asks each
account's own adapter for its approved list (`list_templates`) and writes local rows as
a **read-only reflection**, using `db_insert` / `db_update` to bypass frappe_whatsapp's
Meta-bound validate/after_insert entirely. No Meta, no push back to the provider.
"""
import frappe

from tatva_connect.channels import resolve
from tatva_connect.whatsapp import channel, roles


@frappe.whitelist()
def sync_templates(account_name=None):
	"""Reflect approved provider templates into WhatsApp Templates. Returns per-account counts.

	With no `account_name`, syncs EVERY account (each tenant has its own template namespace).
	Records are keyed per account so two tenants can share a template name without clobbering
	each other.
	"""
	from crm.api.whatsapp import validate_access
	validate_access()  # WhatsApp capability gate (reads whatsapp_capability_roles) — a User refreshes its own account
	if account_name:
		accounts = [account_name]
	else:
		frappe.only_for(roles.ADMIN_ROLES)  # the all-accounts sweep is WhatsApp Admin / scheduler only (the rep UI always passes an account)
		accounts = frappe.get_all("WhatsApp Account", filters={"status": "Active"}, pluck="name")  # authz-ok: gated above; enumerates config accounts, not user records
	if not accounts:
		frappe.throw("No active WhatsApp Account found to sync templates from.")

	totals = {}
	for acc in accounts:
		totals[acc] = _sync_one(acc)
	frappe.db.commit()
	return totals


def scheduled_sync_all():
	"""Scheduler hook (every 6h): refresh every account's templates.

	A good-have backstop on top of the real-time manual sync. Respects the kill-switch and never
	lets a sync failure raise out of the scheduler.
	"""
	from tatva_connect import automation

	if not automation.is_enabled(channel.SWITCH_TEMPLATES):
		return
	if not channel.is_enabled():
		return
	try:
		totals = sync_templates()
		frappe.logger("tatva_connect").info(f"WhatsApp scheduled template sync: {totals}")
	except Exception:
		frappe.log_error(
			title="WhatsApp scheduled template sync failed",
			message=frappe.get_traceback(),
		)


def _record_name(element_name, account_name):
	"""Account-scoped record id so the same template name can exist per tenant."""
	return f"{element_name}::{account_name}"


def _sync_one(account_name):
	account = frappe.get_doc("WhatsApp Account", account_name)
	# The adapter owns "which templates are approved" — the shape of that answer is its provider's,
	# not ours, and the day a second provider spells it differently only its adapter changes.
	items = resolve.adapter_for(account).list_templates(account)

	created = updated = skipped = 0
	for t in items:
		try:
			element_name = t.get("elementName")
			if not element_name:
				continue
			# Sample values, keyed by param name ({"1": "...", "2": "..."}), so the
			# picker can show an exact per-variable hint. The {{N}} -> CRM field
			# mapping lives in `field_names` (operator-set) — never overwritten here.
			custom_params = t.get("customParams") or []
			sample_values = frappe.as_json(
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
				"actual_name": element_name,  # the real provider-side name used to send
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
				title="WhatsApp template sync skipped one",
				message=f"account={account_name} template={t.get('elementName')}\n{frappe.get_traceback()}",
			)

	return {"created": created, "updated": updated, "skipped": skipped}
