# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The reader and the writer must route an activity field by the SAME rule.

`activity/api.py` declares that rule once — `field_target(f)`: the field's `target` is a retained CRM
Task column if it is one of `PROMOTED_COLUMNS`, and otherwise the field lives in the JSON payload under
its schema fieldname. `compute_activity` (write), `set_schema_field` (write) and `smartview/api.py`
(project) all ask it.

`_task_values` — the READ side, which re-keys a saved task back to schema fieldnames — did not. It read
`f.get("target")` raw and did `r.get(target)`. `CRM Task Type Field.target` is an unconstrained `Data`
field: no options, no validation. So an operator may declare a `target` that names a real CRM Task
column which is NOT promoted — and then the two sides disagree about where the answer lives.

When that column is also one the reader SELECTs (`status`, `assigned_to`, `description`, `owner` —
api.py:414, :501, :636), the disagreement is not a blank: the reader hands back the TASK'S OWN unrelated
column value in place of the rep's answer. A "Patient Consented" reads back as "Todo".

Phase 0 of docs/plans/task-form-layer/2026-07-25-task-slots-to-sections-and-form-layer.md. §7 makes this seam the one
every consumer resolves through, so it must be coherent before it is widened.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.activity.test_field_routing_coherence
"""
import itertools
from collections import defaultdict

import frappe
from frappe.model import NO_VALUE_FIELDS
from frappe.tests.utils import FrappeTestCase

from tatva_connect.activity import api as activity_api
from tatva_connect.activity import backfill
from tatva_connect.tests.activity import task_type_fixture

# A type whose rules reference more condition values than this is not evaluated exhaustively; it is
# REPORTED instead, because a silent skip reads as "checked and clean". Nothing on the live seed comes
# close (the widest is a few dozen), so this is a runaway guard and not a sampling policy.
_COMBINATION_CEILING = 20000

TYPE_NAME = "ZZ Routing Coherence Probe"

# Two targets that name a REAL CRM Task column the reader SELECTs but that the writer does not promote.
# This is the whole defect surface: a target the two sides read differently AND that carries a value.
DIVERGENT_TARGETS = ("status", "assigned_to")

# A promoted field and a payload field alongside them — a fixture carrying only the divergent shape
# could not tell a fixed reader from one that simply stopped reading columns.
SCHEMA = (
	{"label": "ZZ Visit Status", "fieldname": "zz_visit_status", "fieldtype": "Data", "target": "status"},
	{"label": "ZZ Handled By", "fieldname": "zz_handled_by", "fieldtype": "Data", "target": "assigned_to"},
	{"label": "ZZ Outcome", "fieldname": "zz_outcome", "fieldtype": "Data", "target": "custom_outcome"},
	{"label": "ZZ Remark", "fieldname": "zz_remark", "fieldtype": "Small Text"},
)

SUBMITTED = {
	"zz_visit_status": "ZZ Patient Consented",
	"zz_handled_by": "ZZ Field Rep",
	"zz_outcome": "ZZ Reached",
	"zz_remark": "ZZ remark text",
}


class TestFieldRoutingCoherence(FrappeTestCase):
	"""One routing rule, asked by both sides — write through `field_column`, read through `field_column`."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.task_type = task_type_fixture.mint_type(TYPE_NAME, SCHEMA)

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		task_type_fixture.teardown()
		super().tearDownClass()

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.set_user("Administrator")
		self.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Routing Coherence Probe",
			"mobile_no": f"+9198129{int(frappe.generate_hash(length=8), 16) % 100000:05d}",
			"custom_vertical": task_type_fixture.VERTICAL, "custom_group": task_type_fixture.GROUP,
		}).insert(ignore_permissions=True)

	def test_the_probe_targets_are_real_non_promoted_task_columns(self):
		"""The premise. A target naming no real column could never collide, and one that WAS a promoted
		column is no divergence at all — either way the fixture would prove nothing."""
		for target in DIVERGENT_TARGETS:
			self.assertTrue(frappe.db.has_column("CRM Task", target),
							f"`{target}` is not a real CRM Task column — the probe cannot collide")
			self.assertNotIn(target, backfill.PROMOTED_COLUMNS,
							 f"`{target}` was a promoted column — pick a target the router sends to a section")

	def test_no_type_shows_two_fields_that_write_the_same_place(self):
		"""THE convergence lock, driven over the LIVE seed — every task type on the site, not a fixture.

		Many LSQ names land on one generic column: `dropped_reasons`, `not_interested_reasons` and three
		more all answer in `engagement.dropped_reason`, and that convergence IS the design — it is what
		lets one worklist column mean the same thing across sixty types.

		It is safe on exactly one condition: **the two fields are never on screen together.** "Introductory
		Meeting" declares `visit_status` and `phone_call_status` against the same column and is fine,
		because its rules show one or the other and never both. Merge two fields a form asks side by side
		and the second write silently overwrites the first — no error, no blank, just a lost answer. That
		is what this refuses, and it caught a real one: "Dropped" showed `reasons` and `other_reasons`
		together while both pointed at `engagement.reasons`.

		Asked of the ENGINE, never of a rule restated here: the addresses come from `field_target` (the one
		router) and what is on screen comes from `_shown_fieldnames` (the one visibility fixpoint the form
		and the save both use). The answers driven through it are the values the type's own rules condition
		on, so the search covers every state the rules can distinguish, and no more."""
		unsafe, unchecked = {}, []
		for name in frappe.get_all("CRM Task Type", pluck="name"):
			task_type = frappe.get_doc("CRM Task Type", name)
			fields = activity_api.compiled_fields(task_type)
			sharing = self._addresses_with_more_than_one_field(fields)
			if not sharing:
				continue
			combinations = self._rule_answer_space(task_type)
			if len(combinations) > _COMBINATION_CEILING:
				unchecked.append(name)
				continue
			together = self._pairs_ever_shown_together(fields, sharing, combinations)
			if together:
				unsafe[name] = together

		self.assertEqual(unchecked, [], "a type's answer space was too wide to check — it is NOT covered")
		self.assertEqual(unsafe, {}, "these fields share one storage address AND appear on the same form "
									 "together, so one silently overwrites the other")

	def test_no_declared_field_is_hidden_with_no_way_to_be_shown(self):
		"""A field the form can NEVER show is a field nobody can answer — and worse, one whose migrated
		value is refused outright.

		Our compile is set-based, so a field hidden at form-onload appears only if some rule Shows it. LSQ
		can hide a field and fill it invisibly with `Set Value`, a verb we do not have; copy that Hide
		across and the field is simply dead. Measured on 2026-07-29: three such fields on Document Upload
		and one on Document Verification refused **893 of 3,663** migrated activities in a single run,
		each with "was not shown on this form and its value cannot be saved".

		Asked of the ENGINE over the LIVE seed: for every declared field, is there ANY answer state the
		type's own rules can distinguish in which `_shown_fieldnames` returns it? If not, the declaration
		promises a question that can never be put."""
		dead = {}
		for name in frappe.get_all("CRM Task Type", pluck="name"):
			task_type = frappe.get_doc("CRM Task Type", name)
			fields = activity_api.compiled_fields(task_type)
			# Only a RULE-driven type is judged here: the answer space is derived from the rules, so a type
			# whose fields carry their own hand-written `depends_on` has no space to search and every
			# conditional field would read as unreachable. Those are covered by test_rule_compilation.
			if not fields or not (task_type.get("rules") or []):
				continue
			reachable = set()
			for answers in self._rule_answer_space(task_type):
				reachable |= activity_api._shown_fieldnames(fields, answers)
			unreachable = sorted({f.fieldname for f in fields} - reachable)
			if unreachable:
				dead[name] = unreachable

		self.assertEqual(dead, {}, "these declared fields can never appear on their form, so they can "
								   "neither be answered by a rep nor accepted from a migration")

	def _addresses_with_more_than_one_field(self, fields):
		"""{address: [fieldnames]} for every storage address this type points at more than once."""
		at = defaultdict(list)
		for f in fields:
			at[activity_api.field_target(f)].append(f.fieldname)
		return {address: names for address, names in at.items() if len(names) > 1}

	def _rule_answer_space(self, task_type):
		"""Every combination of answers the type's own rules can tell apart, blank included.

		Bounded by the RULES rather than by the schema: a condition nothing tests cannot change what is
		shown, so enumerating its options would only multiply the search without widening it."""
		options = defaultdict(set)
		for row in task_type.get("rules") or []:
			field = (row.condition_field or "").strip()
			if field:
				options[field].add((row.condition_value or "").strip())
				options[field].add("")
		names = sorted(options)
		return [dict(zip(names, combo, strict=True)) for combo in itertools.product(*[sorted(options[n]) for n in names])] or [{}]

	def _pairs_ever_shown_together(self, fields, sharing, combinations):
		"""The fieldnames of one address that some answer state puts on screen at the same time."""
		found = {}
		for answers in combinations:
			shown = activity_api._shown_fieldnames(fields, answers)
			for address, names in sharing.items():
				overlap = sorted(set(names) & shown)
				if len(overlap) > 1:
					found.setdefault(str(address), set()).update(overlap)
		return {address: sorted(names) for address, names in found.items()}

	def test_the_lock_above_can_actually_fail(self):
		"""A lock that cannot go red is decoration. Two fields the form shows together, aimed at one
		address: the check must name them."""
		fields = [
			frappe._dict(fieldname="zz_a", fieldtype="Data", target="custom_outcome", section="",
						 depends_on="", container_depends_on=[], mandatory_depends_on="", reqd=0),
			frappe._dict(fieldname="zz_b", fieldtype="Data", target="custom_outcome", section="",
						 depends_on="", container_depends_on=[], mandatory_depends_on="", reqd=0),
		]
		sharing = self._addresses_with_more_than_one_field(fields)

		self.assertEqual(len(sharing), 1, "two fields aimed at one column were not seen as sharing it")
		self.assertEqual(self._pairs_ever_shown_together(fields, sharing, [{}]),
						 {"(None, 'custom_outcome')": ["zz_a", "zz_b"]},
						 "the check did not report two unconditional fields sharing one column")

	def test_a_layout_row_is_not_a_field_that_can_collide(self):
		"""Section and Column Breaks store nothing, so they must never be counted as sharing an address —
		otherwise every type with two Section Breaks would read as a collision and the lock would be noise."""
		compiled = activity_api.compiled_fields(frappe.get_doc("CRM Task Type", self.task_type))

		self.assertTrue(all(f.fieldtype not in NO_VALUE_FIELDS for f in compiled),
						"compiled_fields handed back a layout row — the lock would count it as storage")

	def test_every_saved_answer_reads_back_as_the_answer(self):
		"""The property, on the paths the rep actually uses: `save_activity` writes, `task_detail` reads.
		For EVERY field the type declares, the place the reader looks is the place the writer put it.
		Before the fix `zz_visit_status` comes back as the task's own status ('Todo')."""
		task = activity_api.save_activity(self.lead.name, self.task_type, SUBMITTED)
		values = activity_api.task_detail(task)["task"]["values"]
		for fieldname, submitted in SUBMITTED.items():
			self.assertEqual(
				values.get(fieldname), submitted,
				f"`{fieldname}` was written to one place and read from another — the reader handed back "
				f"{values.get(fieldname)!r} where the rep answered {submitted!r}",
			)
