"""WATI override of frappe_whatsapp's `WhatsApp Templates`.

Upstream creates/edits/fetches templates ON META (`after_insert`,
`update_template`, media upload, the module-level `fetch`). For WATI this is
wrong and dangerous: templates live on WATI and are managed there. We never
create or edit a template from Frappe.

So for WATI we neutralise every Meta-bound path:
- `validate` keeps only the harmless local bits (account + language_code);
- `after_insert` / `update_template` become no-ops.

The local `WhatsApp Templates` rows are a READ-ONLY mirror of WATI's
getMessageTemplates (populated by tatva_connect.whatsapp.templates_sync, Phase 3),
so the CRM picker has something to list. Registered via override_doctype_class.
"""
import frappe
from frappe import _
from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_templates.whatsapp_templates import (
	WhatsAppTemplates,
)


class WATITemplates(WhatsAppTemplates):
	def validate(self):
		# Templates live on WATI and are mirrored (sync uses db_insert/db_update,
		# which bypass this validate). We BLOCK manual creation (no phantom local
		# templates) but ALLOW editing an existing row so operators can set the
		# `field_names` variable->lead-field mapping. We never push to WATI/Meta
		# (after_insert/update_template are no-ops), and the synced fields (body,
		# actual_name, etc.) get refreshed on the next sync regardless.
		if self.is_new():
			frappe.throw(
				_(
					"WhatsApp Templates are managed on WATI and mirrored read-only. "
					"Manual creation is disabled — use the 'Sync from WATI' button to import them."
				),
				title=_("Templates are read-only (WATI)"),
			)

	def after_insert(self):
		# Defensive no-op (db_insert never calls this; manual insert is blocked in validate).
		pass

	def update_template(self):
		# Never edit the template on Meta.
		pass

	def on_trash(self):
		# No-Meta backstop: upstream on_trash POSTs a DELETE to the provider's
		# message_templates endpoint. Our rows are a read-only mirror of WATI —
		# deleting one locally must never call out. No-op.
		pass
