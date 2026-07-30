# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The four `CRM Task` list lenses offer the declaration's fields and nothing else.

`/crm/tasks/view/list` has four field menus — Filter, Group By, Sort By and Columns. Three are served
by native crm endpoints that walk raw `frappe.get_meta("CRM Task").fields`; the fourth reads doctype
meta in the browser. All four therefore offered a rep the nine shared slots ("Key Date 1" — which of
the 27 task types' answers?) and the operational plumbing (workflow token, LSQ ids, notified-for
stamps, GPS capture, ASM).

The property under test is not "those names are hidden". It is that a lens **resolves through the ONE
declaration** — `CRM Task.default_list_data()["rows"]`, plan §6 — so it is the native answer
INTERSECTED with the declaration and nothing else. That is asserted directly (`lens == declared &
native`), which a suppression list could not satisfy: it would also have to keep every field the
declaration omits but nobody thought to suppress.

The lenses are resolved through `frappe.override_whitelisted_method`, exactly as an HTTP request
resolves them, so this also proves the `hooks.py` wiring and not merely that the module exists.

RED before Phase 6: with no override registered the map resolves to the native function, and
`test_no_slot_or_operational_field_reaches_a_task_lens` fails on the first forbidden name it finds. It
found `custom_key_date_1` then; Phase 7 has since dropped all six SLOTS from the table, so the RED now
comes off the OPERATIONAL columns, which are still real. SLOTS stays declared here so a column that ever
comes back cannot come back into a rep's menu unnoticed.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.tasks.test_list_lenses
"""

import json

import frappe
from frappe.model.document import get_controller
from frappe.tests.utils import FrappeTestCase

from tatva_connect.list_engine import derived

TASK = "CRM Task"

# The three lenses with a native endpoint. Named by their crm path — the test resolves each through the
# override map, so it reads the same answer the browser gets.
NATIVE_LENSES = (
	"crm.api.doc.get_filterable_fields",
	"crm.api.doc.get_group_by_fields",
	"crm.api.doc.sort_options",
)

# The fourth. Its picker has no native endpoint (ColumnSettings.vue reads doctype meta), so this
# endpoint IS the lens and there is nothing to override.
COLUMN_LENS = "tatva_connect.api.task_lenses.get_column_fields"

# Doctypes that must be untouched by this layer. FCRM Note and CRM Call Log are listed alongside the
# two big ones because they share the same endpoints and would drift silently.
OTHER_DOCTYPES = ("CRM Lead", "CRM Deal", "FCRM Note", "CRM Call Log")

# What a rep may never be offered. This is the TEST's statement of plan §6 — the expectation lives here
# precisely because the code may not carry such a list.
SLOTS = (
	"custom_key_date_1",
	"custom_key_date_2",
	"custom_key_date_3",
	"custom_key_date_4",
	"custom_reference",
	"custom_activity_payload",
)
OPERATIONAL = (
	"custom_automated",
	"custom_lsq_task_id",
	"custom_lsq_activity_id",
	"custom_external_id",
	"custom_workflow_token",
	"custom_due_soon_notified_for",
	"custom_overdue_notified_for",
	"custom_location_latitude",
	"custom_location_longitude",
	"custom_location_address",
	"custom_location_accuracy_m",
	"custom_location_captured_at",
	"custom_location_geo",
	"custom_asm",
	"custom_checklist",
)

# Plan §6's rep-facing set, verbatim. The declaration must name every one of these or a rep loses a
# question they can legitimately ask of the list.
REP_FACING = {
	"name",
	"custom_task_type",
	"title",
	"reference_docname",
	"reference_doctype",
	"status",
	"priority",
	"due_date",
	"start_date",
	"custom_completed_on",
	"assigned_to",
	"custom_outcome",
	"custom_followup_at",
	"custom_scheduled_at",
	"description",
}


def _dispatched(cmd):
	"""The function an HTTP call to `cmd` would actually run — the override when one is registered."""
	return frappe.get_attr(frappe.override_whitelisted_method(cmd))


def _native(cmd):
	"""The upstream function, imported directly, never through the override map."""
	return frappe.get_attr(cmd)


def _names(rows):
	return {r.get("fieldname") for r in rows}


def _stable(rows):
	"""A byte-comparable form of a lens answer — order and every key preserved."""
	return json.dumps(rows, sort_keys=True, default=str)


class TestTaskListLenses(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.declared = set(get_controller(TASK).default_list_data().get("rows") or [])
		# The second declaration: fields that exist only in `list_engine/fields.py`, never in doctype meta.
		cls.derived = set(derived.names(TASK))

	def test_the_declaration_names_the_whole_rep_facing_set(self):
		"""The premise of every other test: the lenses resolve through the declaration, so the
		declaration must BE plan §6. A field missing here silently disappears from all four menus."""
		missing = REP_FACING - self.declared
		self.assertEqual(
			missing, set(), f"CRM Task.default_list_data() omits rep-facing fields: {sorted(missing)}"
		)

	def test_the_declaration_names_no_slot_and_no_operational_column(self):
		"""The other half of the premise. Nothing can filter a slot out downstream if it is declared."""
		leaked = self.declared & (set(SLOTS) | set(OPERATIONAL))
		self.assertEqual(leaked, set(), f"the declaration itself names fields §6 excludes: {sorted(leaked)}")

	def test_the_native_lenses_really_do_offer_something_the_declaration_excludes(self):
		"""Proves the fixture is real. If the native lenses offered nothing §6 excludes, the narrowing test
		below would pass for a reason that is no achievement of ours, and we would never know.

		Phase 7 DROPPED the six SLOTS from the table, so the native pickers cannot offer them any more and
		this premise now rests on the OPERATIONAL columns — real columns of CRM Task that plan §6 keeps out
		of every rep-facing menu. Widening it here rather than dropping it keeps the premise falsifiable."""
		for cmd in NATIVE_LENSES:
			offered = _names(_native(cmd)(TASK))
			self.assertTrue(
				offered & (set(SLOTS) | set(OPERATIONAL)),
				f"{cmd} no longer offers anything §6 excludes — this test's premise is gone, re-read plan §6",
			)

	def test_no_slot_or_operational_field_reaches_a_task_lens(self):
		"""The rep-facing property. Every one of the four menus, every forbidden name."""
		forbidden = set(SLOTS) | set(OPERATIONAL)
		for cmd in (*NATIVE_LENSES, COLUMN_LENS):
			leaked = _names(_dispatched(cmd)(TASK)) & forbidden
			self.assertEqual(leaked, set(), f"{cmd} still offers a rep: {sorted(leaked)}")

	def test_each_lens_is_the_declaration_intersected_with_the_native_answer(self):
		"""The DESIGN, not just its effect. A lens offers exactly `(declared & native) | derived`: nothing
		outside a declaration (so a new doctype column is invisible until declared), and nothing the native
		lens itself rules out (fieldtype eligibility and standard fields stay upstream's business).
		A suppression list cannot satisfy the first half — it would leak every undeclared field nobody
		remembered to name.

		The derived half is the second declaration, not an exception to the first. A derived field is not in
		`frappe.get_meta`, so no native lens can find it and the intersection could never contain it; it is
		offered because `list_engine/fields.py` declares it, on exactly the same terms."""
		for cmd in NATIVE_LENSES:
			offered = _names(_dispatched(cmd)(TASK))
			expected = (self.declared & _names(_native(cmd)(TASK))) | self.derived
			self.assertTrue(offered, f"{cmd} offers nothing at all for {TASK}")
			self.assertEqual(offered, expected, f"{cmd} is not the declarations intersected with native")

	def test_a_derived_field_is_offered_only_where_it_is_declared(self):
		"""The derived half narrows too: a doctype that declares no derived field is offered none."""
		self.assertIn("due_state", _names(_dispatched(NATIVE_LENSES[0])(TASK)))
		for doctype in OTHER_DOCTYPES:
			with self.subTest(doctype):
				self.assertNotIn("due_state", _names(_dispatched(NATIVE_LENSES[0])(doctype)))

	def test_the_column_lens_is_the_declaration_too(self):
		"""The fourth menu, whose picker has no native endpoint to intersect with — it is fed by the
		same narrowed answer the filter lens returns, so all four have one source."""
		offered = _names(_dispatched(COLUMN_LENS)(TASK))
		self.assertTrue(offered, "the column lens offers nothing at all for CRM Task")
		self.assertLessEqual(
			offered, self.declared | self.derived, "the column lens offers fields no declaration names"
		)
		self.assertEqual(offered, _names(_dispatched(NATIVE_LENSES[0])(TASK)))

	def test_grouping_by_task_type_is_still_possible(self):
		"""Narrowing must not take away the one grouping Phase 6 exists to make readable."""
		self.assertIn("custom_task_type", _names(_dispatched("crm.api.doc.get_group_by_fields")(TASK)))

	def test_every_other_doctype_is_byte_identical_to_upstream(self):
		"""No cross-impact. For anything that is not CRM Task the override returns the native answer
		unchanged — same entries, same order, same keys."""
		for doctype in OTHER_DOCTYPES:
			for cmd in NATIVE_LENSES:
				self.assertEqual(
					_stable(_dispatched(cmd)(doctype)),
					_stable(_native(cmd)(doctype)),
					f"{cmd} changed for {doctype} — this layer narrows CRM Task only",
				)

	def test_the_column_lens_stands_down_for_every_other_doctype(self):
		"""An empty answer is ColumnSettings.vue's own contract for "keep the stock meta source", so a
		doctype that declares no rep-facing set keeps its picker exactly as upstream ships it."""
		for doctype in OTHER_DOCTYPES:
			self.assertEqual(_dispatched(COLUMN_LENS)(doctype), [], f"the column lens narrowed {doctype}")
