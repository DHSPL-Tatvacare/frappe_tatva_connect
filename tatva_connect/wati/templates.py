"""WATI override of frappe_whatsapp's `WhatsApp Templates`.

Upstream creates/edits/fetches templates ON META (`after_insert`,
`update_template`, media upload, the module-level `fetch`). For WATI this is
wrong and dangerous: templates live on WATI and are managed there. We never
create or edit a template from Frappe.

So for WATI we neutralise every Meta-bound path:
- `validate` keeps only the harmless local bits (account + language_code);
- `after_insert` / `update_template` become no-ops.

The local `WhatsApp Templates` rows are a READ-ONLY mirror of WATI's
getMessageTemplates (populated by tatva_connect.wati.templates_sync, Phase 3),
so the CRM picker has something to list. Registered via override_doctype_class.
"""
import frappe
from frappe import _
from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_templates.whatsapp_templates import (
	WhatsAppTemplates,
)


class WATITemplates(WhatsAppTemplates):
	def validate(self):
		# Read-only catalog. Templates live on WATI; we only mirror them (via
		# tatva_connect.wati.templates_sync, which writes with db_insert/db_update
		# and therefore bypasses this validate). Any ORM save — a manual "Create
		# New Template", an edit, or upstream's Meta fetch — is blocked here so we
		# never create a phantom local template or touch Meta.
		frappe.throw(
			_(
				"WhatsApp Templates are managed on WATI and mirrored read-only. "
				"Manual create/edit is disabled — use the 'Sync from WATI' button to refresh."
			),
			title=_("Templates are read-only (WATI)"),
		)

	def after_insert(self):
		# Defensive no-op (db_insert never calls this; manual insert is blocked in validate).
		pass

	def update_template(self):
		# Never edit the template on Meta.
		pass
