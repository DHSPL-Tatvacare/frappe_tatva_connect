"""The logging seam: nothing reaches `tabError Log` carrying a plaintext secret.

Masking at our own log call sites cannot work, because the leak happens above them. `make_request`
(frappe/integrations/utils.py) calls a bare `frappe.log_error()` inside its OWN except block, so a
request whose URL carries an App Secret is written out before any handler in this app is reached.

`frappe.log_error` builds a real Error Log Document and inserts it (frappe/utils/error.py), and the
deferred lane flushes through `frappe.get_doc(record).insert()` (frappe/deferred_insert.py), so both
lanes resolve this controller and both are masked by the one override. It is bound through
`override_doctype_class` rather than `doc_events`: masking is infrastructure, not an operator
automation, so it carries no CRM Tatva Automation row and is never dormant. `WhatsApp Account` is
bound the same way for the same reason.

Three fields can carry a secret. `error` holds the traceback, which `get_traceback(with_context=True)`
fills with locals and with the failing URL. `method` holds the title, and frappe folds an over-long
method INTO the error, so the same text can arrive under either. `metadata` holds the request path or
the background job's kwargs, and a job argument can be a token.

Known limitation, not solved here: `capture_exception` (frappe/utils/sentry.py) runs BEFORE the insert
and so sees the unmasked text. It returns early unless the `enable_telemetry` system setting is on,
and neither that nor a Sentry DSN is set on this deployment, so nothing leaves today.
"""
import frappe
from frappe.core.doctype.error_log.error_log import ErrorLog

from tatva_connect.utils import mask_secrets

MASKED_FIELDS = ("error", "method", "metadata")


class MaskedErrorLog(ErrorLog):
	def validate(self):
		"""Mask last: core's own validate moves an over-long method into the error, so masking before it
		would leave the moved copy standing."""
		super().validate()
		for fieldname in MASKED_FIELDS:
			value = self.get(fieldname)
			if not value:
				continue
			try:
				self.set(fieldname, mask_secrets(value))
			except Exception:
				# A masker that raised would take the whole error log with it, losing the incident as well.
				frappe.logger("tatva_connect").warning(f"Secret masking failed for Error Log.{fieldname}")
