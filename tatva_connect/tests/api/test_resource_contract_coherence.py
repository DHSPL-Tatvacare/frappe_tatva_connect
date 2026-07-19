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
from tatva_connect.tests.api import partner_fixture

METRICS = "CRM Lead Activity Metrics"
METRICS_SECTION = "metrics"  # the section was renamed mx->metrics; the field_key prefix is `metrics:`

PARTNER = "coherence.fixture.partner@example.test"
ANCHOR = "lead:mobile_no"  # the dedup anchor `_allowed_keys` adds itself — our rule, not a tick

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
		for name, _module, specs, doctype in RESOURCES:
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
		kept from a CALLER by not being ticked — not by code reinterpreting a Frappe flag.

		Scoped to PARTNER contracts (is_internal=0): a partner may not tick a computed metric (it would let
		it overwrite a CRM-derived number). The per-grain INTERNAL VISIBILITY contracts (is_internal=1)
		legitimately DO tick metrics — reps READ them on the Data tab — so those are not callers and are
		excluded here.

		This asserts the tick, not `_allowed_keys`: an EMPTY grid resolves to the full catalog by design
		(`_allowed_keys`: "Empty grid, or System Manager -> full catalog"), so a contract that restricts
		nothing would fail this for a reason that is not the metrics. That fail-open default is a real
		question and it is not this phase's."""
		partner_contracts = frappe.get_all(
			"CRM Lead API Mapping", filters={"is_internal": 0}, pluck="name"
		)
		self.assertEqual(
			frappe.get_all(
				"CRM Lead API Mapping Field",
				filters={"field": ["like", f"{METRICS_SECTION}:%"], "parent": ["in", partner_contracts]},
				pluck="field",
			),
			[],
		)

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


class TestTheContractIsTheAllowlist(FrappeTestCase):
	"""2.8 — the rule under everything: what a caller may send is what its contract ticks, and nothing
	else. Asserted on a contract THIS class minted.

	It used to be asserted as three numbers read off the dev site — "Anaya has 118 keys". That is the
	seed's answer, not the code's: it moves when an operator ticks a box, and it says nothing about
	whether `_allowed_keys` honours a tick. Both branches of that function are pinned here instead."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.ticked = [partner_fixture.mint_catalog_row(fn) for fn in ("zzprobe_a", "zzprobe_b")]
		cls.unticked = partner_fixture.mint_catalog_row("zzprobe_c")  # minted, deliberately NOT ticked
		partner_fixture.mint_partner(PARTNER, ticks=cls.ticked)
		frappe.db.commit()  # survives the per-test rollback; _allowed_keys reads the contract live

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		partner_fixture.teardown()
		frappe.db.commit()
		frappe.cache().delete_value(partner._CATALOG_CACHE_KEY)
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		# The bust is direct, never clear_catalog_cache(): that hook is gated on a dormant switch.
		frappe.cache().delete_value(partner._CATALOG_CACHE_KEY)
		self.addCleanup(frappe.cache().delete_value, partner._CATALOG_CACHE_KEY)

	def test_a_ticked_grid_resolves_to_exactly_those_keys_plus_the_anchor(self):
		"""The contract IS the allowlist. Two of three minted keys are ticked, so two come back — plus
		`lead:mobile_no`, which is ours: the dedup anchor rides free on every contract, never a tick."""
		got = set(partner._allowed_keys(PARTNER, True))
		self.assertEqual(got, set(self.ticked) | {ANCHOR})
		self.assertNotIn(self.unticked, got, "an unticked key reached the caller")

	def test_an_empty_grid_resolves_to_the_whole_catalog(self):
		"""The other branch, as `_allowed_keys` documents it: "Empty grid, or System Manager -> full
		catalog". That fail-open default is a real question and it is not this phase's — but it IS the
		behaviour, so it is pinned rather than left for an empty contract to discover in production."""
		frappe.db.delete("CRM Lead API Mapping Field", {"parent": partner._contract_name(PARTNER)})
		self.addCleanup(frappe.db.rollback)
		got = set(partner._allowed_keys(PARTNER, True))
		self.assertEqual(got, set(partner._catalog()["keys"]))
		self.assertIn(self.unticked, got, "an empty grid must fall open to the whole catalog")
