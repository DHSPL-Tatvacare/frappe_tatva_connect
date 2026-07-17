# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Discovery and ingestion must read the SAME object.

`field_spec` is the contract layer: one `FieldSpec` declaration, two readers. `describe` is what a
`*_schema` endpoint advertises; `collect` is what a write path accepts. They cannot drift, because
neither owns a field list — both iterate the same specs.

The two rules with teeth:
  * `collect` iterates SPECS, never `data.keys()` — so a caller can never inject a field we did not
    declare. An undeclared key is dropped in silence, not thrown at.
  * a `read_only` spec is OUTPUT_ONLY to `describe` and invisible to `collect` — a computed column is
    discoverable but never writable.

And `describe` reads LIVE meta: a `target` that is not a real column throws, rather than degrading to
"Data" and advertising a lie.

The one thing live meta must NOT be trusted for is the vocabulary of a Select. `direction` lands on
`CRM Call Log.type`, which holds `Incoming/Outgoing` — but the partner's vocabulary is
`Inbound/Outbound`. A resource that declares its own `allowed_values` means the internal one is not the
partner's business, so it is suppressed.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.api._base import BEHAVIOR_OUTPUT_ONLY, EXTERNAL_ID_FIELD, field_descriptor
from tatva_connect.api.field_spec import FieldSpec, collect, describe

NOTE = "FCRM Note"
CALL = "CRM Call Log"

# NOTE_FIELDS as it stands today. Frozen HERE and deliberately NOT imported: Phase 2 rewrites the real
# tuple into specs, and a "before" that drifts with the "after" locks nothing.
_NOTE_TODAY = (
	("lead",        "Lead",        "reference_docname", False),
	("mobile_no",   "Mobile No",   None,                False),
	("external_id", "External ID", EXTERNAL_ID_FIELD,   False),
	("title",       "Title",       "title",             False),
	("content",     "Content",     "content",           True),
	("created_at",  "Created At",  None,                False),
)


class TestFieldSpec(FrappeTestCase):
	def test_describe_of_nothing_is_nothing(self):
		"""1.1 — no specs, no descriptors."""
		self.assertEqual(describe([]), [])

	def test_describe_invents_no_behaviour(self):
		"""1.2 — describe is field_descriptor fed from live meta, and nothing else."""
		field = frappe.get_meta(NOTE).get_field("content")
		expected = field_descriptor("content", "Content", field.fieldtype, True, field.options)
		spec = FieldSpec("content", "Content", "content", required=True)
		self.assertEqual(describe([spec], NOTE), [expected])

	def test_describe_calls_an_undeclared_non_column_data(self):
		"""1.13 — a non-column the resource handles and nobody typed falls back to Data."""
		self.assertEqual(describe([FieldSpec("mobile_no", "Mobile No")], NOTE)[0]["type"], "Data")

	def test_describe_types_a_non_column_from_the_spec(self):
		"""1.12 — meta cannot type what is not a column, so here the spec is the ONLY source."""
		got = describe([FieldSpec("created_at", "Created At", fieldtype="Datetime")], NOTE)[0]
		self.assertEqual(got["type"], "Datetime")

	def test_describe_throws_when_a_target_and_a_fieldtype_are_both_declared(self):
		"""1.14 — meta already types a column. A second copy must be impossible to write, not discouraged."""
		spec = FieldSpec("content", "Content", "content", fieldtype="Data")
		with self.assertRaises(frappe.ValidationError):
			describe([spec], NOTE)

	def test_collect_iterates_specs_not_data(self):
		"""1.3 — an undeclared key is DROPPED, silently. This is why a caller cannot inject."""
		specs = (FieldSpec("content", "Content", "content"),)
		got = collect(specs, {"content": "hi", "lead_owner": "attacker@evil.com", "bogus": 1})
		self.assertEqual(got, {"content": "hi"})

	def test_collect_skips_a_read_only_spec(self):
		"""1.4 — a computed field is not writable, however hard the caller pushes."""
		specs = (FieldSpec("count", "Count", "custom_rnr_count", read_only=True),)
		self.assertEqual(collect(specs, {"count": 5}), {})

	def test_describe_marks_a_read_only_spec_output_only(self):
		"""1.5 — discoverable, never required, never accepted."""
		spec = FieldSpec("content", "Content", "content", required=True, read_only=True)
		got = describe([spec], NOTE)[0]
		self.assertEqual(got["behavior"], BEHAVIOR_OUTPUT_ONLY)
		self.assertFalse(got["required"])

	def test_collect_maps_fieldname_to_target(self):
		"""1.6 — the public name is not the column name."""
		specs = (FieldSpec("lead", "Lead", "reference_docname"),)
		self.assertEqual(collect(specs, {"lead": "CRM-LEAD-1"}), {"reference_docname": "CRM-LEAD-1"})

	def test_describe_throws_on_a_target_that_is_not_a_column(self):
		"""1.7 — live meta, fail loud. A silent degrade to Data is how a schema starts lying."""
		spec = FieldSpec("ghost", "Ghost", "not_a_real_column", NOTE)
		with self.assertRaises(frappe.ValidationError):
			describe([spec])

	def test_describe_prefers_a_declared_vocabulary(self):
		"""1.8 — a declared vocabulary wins, and the internal options are suppressed with it."""
		spec = FieldSpec("direction", "Direction", "type", CALL, allowed_values=("Inbound", "Outbound"))
		got = describe([spec])[0]
		self.assertEqual(got["allowed_values"], ["Inbound", "Outbound"])
		self.assertIsNone(got["options"])

	def test_describe_derives_a_selects_vocabulary(self):
		"""1.9 — no declaration: a Select's own options ARE the vocabulary, so nobody re-types them."""
		native = frappe.get_meta(CALL).get_field("status").options
		got = describe([FieldSpec("status", "Status", "status")], CALL)[0]
		self.assertEqual(got["allowed_values"], [o for o in native.split("\n") if o])
		self.assertEqual(got["options"], native)

	def test_describe_gives_a_non_select_no_vocabulary(self):
		"""1.10 — allowed_values is a Select's business; nothing else carries the key at all."""
		self.assertNotIn("allowed_values", describe([FieldSpec("content", "Content", "content")], NOTE)[0])

	def test_describe_reproduces_call_schema_exactly(self):
		"""1.11 — the vocabulary lock. `direction` lands on a column holding Incoming/Outgoing, so a
		describe that trusted meta would advertise our internals. Expected is hand-built from what
		call_schema emits today (never imported): Phase 2 must be able to swap it in unchanged."""
		status_f = frappe.get_meta(CALL).get_field("status")
		expected = [
			field_descriptor("direction", "Direction", "Select", True, None, ["Inbound", "Outbound"]),
			field_descriptor(
				"status", "Status", status_f.fieldtype, False, status_f.options,
				[o for o in (status_f.options or "").split("\n") if o],
			),
		]
		specs = (
			FieldSpec("direction", "Direction", "type", required=True, allowed_values=("Inbound", "Outbound")),
			FieldSpec("status", "Status", "status"),
		)
		got = describe(specs, CALL)
		self.assertEqual(got, expected)
		self.assertNotIn("Incoming", frappe.as_json(got))  # the internal vocabulary never reaches a partner

	def test_describe_reproduces_note_schema_exactly(self):
		"""1.15 — the note lock. Expected is today's note_schema algorithm, hand-run here (never imported)
		over the frozen rows above, so Phase 2 can swap `describe(SPECS)` in and prove nothing moved."""
		m = frappe.get_meta(NOTE)
		expected = []
		for fieldname, label, target, required in _NOTE_TODAY:
			f = m.get_field(target) if target else None
			fieldtype = f.fieldtype if f else "Data"
			if fieldname == "created_at":
				fieldtype = "Datetime"
			expected.append(
				field_descriptor(fieldname, label, fieldtype, required, (f.options if f else None), None)
			)
		specs = tuple(
			FieldSpec(fn, lb, tg, required=rq, fieldtype="Datetime" if fn == "created_at" else None)
			for fn, lb, tg, rq in _NOTE_TODAY
		)
		self.assertEqual(describe(specs, NOTE), expected)
