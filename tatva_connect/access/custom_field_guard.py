# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A Custom Field's fieldname becomes a SQL column, so it must be a legal identifier — lowercase
letters, digits and underscore, nothing else. A fieldname carrying <, >, =, a space or ( is how a
broken-schema / stored-XSS row gets planted, and it surfaces as a failed `bench migrate`
(validate_fields_for_doctype rejects the whole doctype's meta at sync_fixtures).

Frappe already rejects this on the ordinary save — but an insert with `flags.ignore_validate` SKIPS
`run_method("validate")` entirely (document.py:1352), which is the path that plants one. So this runs on
BEFORE_VALIDATE, which frappe runs BEFORE that ignore-return (document.py: "before_validate should be
executed before ignoring validations") — no ORM write can store an illegal fieldname, ignored validate
or not. A raw `db.set_value` bypasses every hook and is out of reach of any guard: that needs
DB-console access, not a remote caller.
"""
import re

import frappe
from frappe import _

# The SQL-identifier allowlist. Verified against every existing Custom Field (0 legitimate rows fail it).
_LEGAL_FIELDNAME = re.compile(r"^[a-z0-9_]+$")


def guard_custom_field(doc, method=None):
	"""Reject a Custom Field whose fieldname is not a legal SQL identifier — on before_validate, so an
	`ignore_validate` insert cannot slip one past the way it did on UAT."""
	fieldname = (doc.fieldname or "").strip()
	if fieldname and not _LEGAL_FIELDNAME.match(fieldname):
		frappe.throw(
			_("Illegal fieldname {0} — a field name may contain only lowercase letters, digits and underscore.").format(
				frappe.bold(fieldname)
			),
			frappe.ValidationError,
			title=_("Invalid Field"),
		)
