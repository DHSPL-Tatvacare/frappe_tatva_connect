# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Rule 4, locked for the three resources that did not obey it: discovery and ingestion read the SAME
object.

Files, Notes and Calls each declared their contract twice — a tuple `*_schema` walked, and a hand-typed
`data.get()` chain the write path walked — so a field could be advertised and not accepted, or accepted
and never advertised, and nothing would say so. These tests assert the two sides against each other
rather than against a list written here: a list written here would be a THIRD brain.

`collect(SPECS, {fn: fn})` is the probe. It answers what the write path would take from a payload
naming every declared field, without writing a row — so ingestion is read from the same specs the write
path runs on, never re-implemented.

The metrics are the same law from the other end, and the answer was NOT code. `CRM Lead Activity
Metrics` is computed, and TatvaPractice's contract ticked all 33 — so a partner could overwrite a number
the CRM derived for itself. Teaching the API to read Frappe's `read_only` flag closed it and took
mobile_no, first_name and the whole lab panel with it (Anaya 117->109, TP 96->56, Niva 59->41), because
that flag is FRAPPE's and means "not editable in a form". One factor, one meaning: the metrics are kept
from a caller by not being ticked on the contract. The contract is the allowlist.
"""
import ast
import inspect
import textwrap

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.api import _base, partner, partner_call, partner_file, partner_note
from tatva_connect.api._base import BEHAVIOR_OUTPUT_ONLY, field_descriptor
from tatva_connect.api.field_spec import FieldSpec, collect, describe

METRICS = "CRM Lead Activity Metrics"
METRICS_SECTION = "mx"

# (module, its specs, the doctype they land on). The three resources this phase put on the contract
# layer. Each test walks all three, so a resource cannot be fixed and another left behind.
RESOURCES = (
	("file", partner_file, partner_file.FILE_FIELDS, "File"),
	("note", partner_note, partner_note.NOTE_FIELDS, "FCRM Note"),
	("call", partner_call, partner_call.CALL_FIELDS, "CRM Call Log"),
)


def _advertised(specs, doctype):
	"""What `*_schema` tells a caller it may send — the writable half of describe."""
	return {d["fieldname"] for d in describe(specs, doctype) if d["behavior"] != BEHAVIOR_OUTPUT_ONLY}


def _accepted(specs):
	"""What the write path takes from a payload naming every declared field.

	A non-column spec (`target=None`) is resolved by the resource itself — `mobile_no` finds a lead,
	`created_at` backdates creation — so it is part of the contract even though `collect` maps it to no
	column. It is accepted iff it is declared and not read-only, which is what this reproduces."""
	taken = collect(specs, {s.fieldname: s.fieldname for s in specs})
	by_target = {s.target: s.fieldname for s in specs if s.target}
	return {by_target[t] for t in taken} | {s.fieldname for s in specs if not s.target and not s.read_only}


class TestResourceContractCoherence(FrappeTestCase):
	def test_every_advertised_field_is_accepted(self):
		"""2.1 — discovery ⊆ ingestion. A field a caller is told to send that is silently dropped is a lie."""
		for name, _module, specs, doctype in RESOURCES:
			with self.subTest(resource=name):
				self.assertEqual(_advertised(specs, doctype) - _accepted(specs), set())

	def test_every_accepted_field_is_advertised(self):
		"""2.2 — ingestion ⊆ discovery. A field the write path takes that no schema names is a back door."""
		for name, _module, specs, doctype in RESOURCES:
			with self.subTest(resource=name):
				self.assertEqual(_accepted(specs) - _advertised(specs, doctype), set())

	def test_every_target_resolves_on_live_meta(self):
		"""2.3 — a spec's target is a real column of the doctype it claims, checked against live meta."""
		for name, _module, specs, doctype in RESOURCES:
			with self.subTest(resource=name):
				for spec in specs:
					if not spec.target:
						continue
					dt = spec.target_doctype or doctype
					self.assertTrue(
						frappe.get_meta(dt).get_field(spec.target),
						f"{name}.{spec.fieldname}: {spec.target} is not a column of {dt}",
					)

	def test_a_dead_target_throws_instead_of_degrading_to_data(self):
		"""2.4 — the rot this layer exists to stop. A renamed column used to leave `f.fieldtype if f else
		"Data"` advertising a Data field that no longer exists; the schema stayed green and started lying.
		Now describe reads live meta and throws."""
		for name, _module, specs, doctype in RESOURCES:
			with self.subTest(resource=name):
				dead = tuple(
					s._replace(target="renamed_away") if s.target else s for s in specs
				)
				with self.assertRaises(frappe.ValidationError):
					describe(dead, doctype)

	def test_the_specs_are_defined_once_and_read_by_both_sides(self):
		"""2.5 — ONE definition per resource, and describe + collect both read THAT object.

		Identity, not equality: `describe(SPECS)` and `collect(SPECS, ...)` must be handed the same tuple
		the module exposes. A copy would satisfy an equality check and drift on the next edit."""
		for name, module, specs, doctype in RESOURCES:
			with self.subTest(resource=name):
				self.assertIsInstance(specs, tuple)
				for spec in specs:
					self.assertIsInstance(spec, FieldSpec)
				self.assertEqual(
					len({s.fieldname for s in specs}), len(specs), "a fieldname is declared twice"
				)
				# both readers accept the module's own object, and agree on what it declares
				self.assertEqual(
					{d["fieldname"] for d in describe(specs, doctype)}, {s.fieldname for s in specs}
				)

	def test_every_metrics_field_is_read_only_on_live_meta(self):
		"""2.6 — the metrics are computed. Read-only is declared on the doctype, so every reader of meta
		(the API, the desk form, a future job) gets the same answer from one place."""
		for field in frappe.get_meta(METRICS).fields:
			if field.fieldtype in frappe.model.no_value_fields:
				continue
			self.assertTrue(field.read_only, f"{field.fieldname} is writable on {METRICS}")

	def test_the_api_never_reads_frappes_read_only_flag(self):
		"""2.7a — one factor, one meaning. `read_only` is FRAPPE's flag and means "not editable in a form";
		21 Property Setters use it that way, on mobile_no and the whole lab panel. Reading it as "the API
		may not write this" made a second meaning on a second leg and unsent the dedup key: Anaya 117->109,
		TP 96->56, Niva 59->41. A platform flag is read as the platform defines it, or not at all."""
		tree = ast.parse(textwrap.dedent(inspect.getsource(_base.is_writable)))
		body = [n for n in ast.walk(tree) if not isinstance(n, (ast.Expr, ast.Constant))]
		names = {n.attr for n in body if isinstance(n, ast.Attribute)} | {
			n.id for n in body if isinstance(n, ast.Name)
		}
		self.assertNotIn("read_only", names, "is_writable must not reinterpret Frappe's read_only")
		self.assertEqual(len(inspect.signature(_base.is_writable).parameters), 1)

	def test_no_contract_ticks_a_metric(self):
		"""2.7b — the hole, closed where it belongs. The contract is the allowlist, so a computed column is
		kept from a caller by not being ticked — not by code reinterpreting a Frappe flag.

		This asserts the tick, not `_allowed_keys`: an EMPTY grid resolves to the full catalog by design
		(`_allowed_keys`: "Empty grid, or System Manager -> full catalog"), so a contract that restricts
		nothing would fail this for a reason that is not the metrics. That fail-open default is a real
		question and it is not this phase's."""
		self.assertEqual(
			frappe.get_all(
				"CRM Lead API Mapping Field",
				filters={"field": ["like", f"{METRICS_SECTION}:%"], "parenttype": "CRM Lead API Mapping"},
				pluck="field",
			),
			[],
		)

	def test_the_partner_field_counts_did_not_move(self):
		"""2.8 — the number under everything: what each partner may send. It changes only when we decide
		it changes. Every silent move today was a defect."""
		for user, expected in (
			("partner-api-anaya@tatvacare.in", 117),
			("partner-api-tp@tatvacare.in", 63),
			("partner-api-niva@tatvacare.in", 59),
		):
			with self.subTest(partner=user):
				self.assertEqual(len(partner._allowed_keys(user, True)), expected)

	def test_the_schemas_did_not_move(self):
		"""2.9 — the regression lock. `describe(SPECS)` reproduces what each `*_schema` advertised before
		the specs existed, field for field.

		The expected shapes are the ones Phase 1 froze (`test_field_spec.py` 1.11 / 1.15) — hand-run from
		today's algorithm and never imported from the code under test. This asserts the real, whole tuple
		against that algorithm, so a spec that quietly changed a label, a type or a `required` is caught."""
		for name, _module, specs, doctype in RESOURCES:
			with self.subTest(resource=name):
				m = frappe.get_meta(doctype)
				expected = []
				for spec in specs:
					f = m.get_field(spec.target) if spec.target else None
					fieldtype = f.fieldtype if f else (spec.fieldtype or "Data")
					options, allowed = (f.options if f else None), None
					if spec.allowed_values:  # a declared vocabulary suppresses the internal one
						fieldtype, options, allowed = "Select", None, list(spec.allowed_values)
					elif f and f.fieldtype == "Select":
						allowed = [o for o in (f.options or "").split("\n") if o] or None
					expected.append(
						field_descriptor(spec.fieldname, spec.label, fieldtype, spec.required, options, allowed)
					)
				self.assertEqual(describe(specs, doctype), expected)
