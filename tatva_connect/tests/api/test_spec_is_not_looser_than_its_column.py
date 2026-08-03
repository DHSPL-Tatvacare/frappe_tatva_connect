# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A SPEC MAY BE LOOSER THAN ITS COLUMN — BUT NEVER THAN A MANDATORY ONE.

`FieldSpec.required` is documented as "the API's contract, which may be looser than the doctype's
reqd", and that is right for an optional column: the API is free to demand less than the table allows.

It is NOT right when the column is mandatory. There, "looser" is a contract the database will refuse
to honour: the schema advertises `required: false`, a partner builds to it, and the write dies inside
frappe's own mandatory check — which answers with the raw `[CRM Call Log, PARTNER-c67e65c380]: from,
to`, naming the doctype, an internal record id, and column names that are not the public ones. The
partner is told to fix a field the schema said was optional, using a name the schema never published.

Measured on UAT: `call_create` without `from_number`/`to_number` did exactly that.

This is a TEST and not a runtime guard on purpose. It is a proof about the source, so it belongs where
proofs belong; a `*_schema` read path must not spend a meta walk per request proving the developer
right, and must never throw at a partner for a mistake only we can fix.
"""
import unittest

import frappe

from tatva_connect.api import partner_call, partner_file, partner_note

# (module, specs attr, default doctype) for every resource declaring its contract as FieldSpec tuples; partner_activity has none — its fields are operator data held in the task-type brain.
RESOURCES = (
	(partner_call, "CALL_FIELDS", "CRM Call Log"),
	(partner_note, "NOTE_FIELDS", "FCRM Note"),
	(partner_file, "FILE_FIELDS", "File"),
)


class TestSpecIsNotLooserThanItsColumn(unittest.TestCase):
	def test_a_mandatory_column_is_declared_required(self):
		offenders = []
		for module, attr, doctype in RESOURCES:
			for spec in getattr(module, attr):
				if not spec.target or spec.read_only or spec.required or spec.supplied:
					continue
				meta = frappe.get_meta(spec.target_doctype or doctype)
				field = meta.get_field(spec.target)
				if field is not None and field.reqd:
					offenders.append(
						f"{module.__name__}.{attr}: `{spec.fieldname}` -> {(spec.target_doctype or doctype)}"
						f".{spec.target} is mandatory, but the spec publishes required=False"
					)
		self.assertEqual(
			offenders, [],
			"A schema advertising a mandatory column as optional cannot be satisfied — the write reaches "
			"frappe's mandatory check and the partner gets a raw error naming internal columns. Declare "
			f"required=True on the spec, or make the column optional: {offenders}",
		)
