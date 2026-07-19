"""Channel override of frappe_whatsapp's `WhatsApp Templates`.

Upstream creates/edits/fetches templates ON META (`after_insert`,
`update_template`, media upload, the module-level `fetch`). For every provider on our
contract this is wrong and dangerous: templates live on the provider and are managed
there. We never create or edit a template from Frappe.

So we neutralise every Meta-bound path:
- `validate` keeps only the harmless local bits (account + language_code);
- `after_insert` / `update_template` become no-ops.

The local `WhatsApp Templates` rows are a READ-ONLY mirror of the provider's own
approved list (populated by tatva_connect.whatsapp.templates_sync), so the CRM picker
has something to list. Registered via override_doctype_class.

Also carries `template_picker_query`, the account-and-grain-aware link-query behind the Send
WhatsApp action's `whatsapp_template` picker.
"""
import frappe
from frappe import _
from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_templates.whatsapp_templates import (
	WhatsAppTemplates,
)


class ChannelWhatsAppTemplates(WhatsAppTemplates):
	def validate(self):
		# Templates live on the provider and are mirrored (sync uses db_insert/db_update,
		# which bypass this validate). We BLOCK manual creation (no phantom local
		# templates) but ALLOW editing an existing row so operators can set the
		# `field_names` variable->lead-field mapping. We never push to the provider
		# (after_insert/update_template are no-ops), and the synced fields (body,
		# actual_name, etc.) get refreshed on the next sync regardless.
		if self.is_new():
			frappe.throw(
				_(
					"WhatsApp Templates are managed on the provider and mirrored read-only. "
					"Manual creation is disabled — use the Sync button to import them."
				),
				title=_("Templates are read-only"),
			)

	def after_insert(self):
		# Defensive no-op (db_insert never calls this; manual insert is blocked in validate).
		pass

	def update_template(self):
		# Never edit the template on Meta.
		pass

	def on_trash(self):
		# No-Meta backstop: upstream on_trash POSTs a DELETE to the provider's
		# message_templates endpoint. Our rows are a read-only mirror of the provider —
		# deleting one locally must never call out. No-op.
		pass


@frappe.whitelist()
def template_picker_query(doctype, txt, searchfield, start, page_length, filters, **kwargs):
	"""Link-query for the Send WhatsApp template picker: one line per template, described by its
	account and the grains routing to that account. Native link-query contract (frappe.desk.search
	build_for_autosuggest): row[0] = value, row[1:] = description. Read-gated (S.1), no raw SQL (S.2)."""
	frappe.has_permission("WhatsApp Templates", "read", throw=True)  # authz-ok: native perm gate on the picker read
	tmpls = frappe.get_all(
		"WhatsApp Templates",
		filters={"actual_name": ["like", f"%{txt}%"]} if txt else {},
		fields=["name", "whatsapp_account"],
		limit_start=int(start or 0), limit_page_length=int(page_length or 20), order_by="actual_name asc",
	)
	accounts = {t.whatsapp_account for t in tmpls if t.whatsapp_account}
	grains = {}
	if accounts:
		for r in frappe.get_all(
			"CRM WhatsApp Routing", filters={"whatsapp_account": ["in", list(accounts)]},
			fields=["whatsapp_account", "vertical", "psp_group", "program"],
		):
			grains.setdefault(r.whatsapp_account, []).append(
				"::".join(x for x in (r.vertical, r.psp_group, r.program) if x)
			)
	return [
		[t.name, t.whatsapp_account or "(no account)", ", ".join(grains.get(t.whatsapp_account, [])) or "(no grain routed)"]
		for t in tmpls
	]
