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
	"""Reflect approved provider templates into WhatsApp Templates. Returns a per-account summary line.

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
	return _summary(totals)


def _summary(totals) -> str:
	"""What the caller is handed: a STRING, because the list view passes it straight to
	`frappe.msgprint`, which searches it for a newline. A dict threw in the browser BEFORE
	`listview.refresh()` ran, so a sync that had already written every row looked like it had failed."""
	lines = [
		f"<b>{acc}</b>: {c['created']} added, {c['updated']} updated"
		+ (f", {c['retired']} retired" if c.get("retired") else "")
		+ (f", {c['skipped']} skipped" if c["skipped"] else "")
		for acc, c in totals.items()
	]
	return "<br>".join(lines) if lines else frappe._("No templates found.")


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
	# The adapter owns "which templates are approved" AND normalizes the answer: it returns the
	# channel's own shape ({name, language, category, body, variables}), so this module reads no
	# provider dictionary. A second provider changes its adapter and nothing here.
	items = resolve.adapter_for(account).list_templates(account)

	created = updated = skipped = 0
	seen = set()
	for t in items:
		try:
			element_name = t.get("name")
			if not element_name:
				continue
			record_name = _record_name(element_name, account_name)
			seen.add(record_name)
			values = {
				# template_name (unique) carries the account-scoped id so two
				# tenants with the same template name don't collide on one row.
				"template_name": record_name,
				"actual_name": element_name,  # the real provider-side name used to send
				"language_code": t.get("language") or "en",
				"status": "APPROVED",
				"category": t.get("category") or "UTILITY",
				"template": t.get("body") or "",
				# Sample values keyed by the provider's own PARAM NAME, so the picker shows an exact
				# per-variable hint and the send matches by name. The {{N}} -> CRM field mapping lives
				# in `field_names` (operator-set) and is never overwritten here.
				"sample_values": frappe.as_json(t.get("variables") or {}),
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
				message=f"account={account_name} template={t.get('name')}\n{frappe.get_traceback()}",
			)

	return {"created": created, "updated": updated, "skipped": skipped,
	        "retired": _retire_absent(account_name, seen)}


def _retire_absent(account_name, seen) -> int:
	"""Hide the rows this account can no longer send, and return how many.

	A MIRROR THAT ONLY EVER ADDS IS NOT A MIRROR. Templates are approved per NUMBER, not per provider
	account: measured on one account, one number listed 235 and its sibling 94, the 94 a strict subset.
	Read without naming the number, both numbers mirrored the same 147 rows — so a rep on the smaller
	number was offered templates it cannot send, and the provider would have refused them in front of a
	patient rather than in the picker.

	RETIRED, NOT DELETED. The picker offers `APPROVED` only, so retiring hides the row from a rep at
	once, while the operator's own {{N}} -> CRM field mapping on it survives the template being approved
	again later. Deleting would throw that work away for a row the provider may well restore.

	Only rows currently APPROVED are touched, so a second sync over the same catalogue writes nothing.
	"""
	stale = [
		row.name
		for row in frappe.get_all(
			"WhatsApp Templates",
			filters={"whatsapp_account": account_name, "status": "APPROVED"},
			fields=["name"],
		)
		if row.name not in seen
	]
	for name in stale:
		frappe.db.set_value("WhatsApp Templates", name, "status", "RETIRED", update_modified=False)
	return len(stale)
