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
from frappe import _
from frappe.model.document import get_controller
from frappe.tests.utils import FrappeTestCase

from tatva_connect.api import task_lenses
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

# Doctypes this layer must not NARROW. FCRM Note and CRM Call Log are listed alongside the two big ones
# because they share the same endpoints and would drift silently.
OTHER_DOCTYPES = ("CRM Lead", "CRM Deal", "FCRM Note", "CRM Call Log")

# The ONE lens that carries a value control, and therefore the only one `_scoped` describes. Group By and
# Sort By pick a field, never a value, so nothing about a control reaches them.
DESCRIBED_LENS = "crm.api.doc.get_filterable_fields"

# The keys `_scoped` may ADD to a native entry. Each describes how a CONTROL reads the column and none is
# a value: which scoped query a composite master is searched by, and which values a grain axis may offer.
STAMPED_KEYS = frozenset({"link_query", "grain_options"})

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
			# Three terms, not two: the field's own `hidden`/`report_hide` is the third, and it is the field's
			# answer rather than this layer's — `reference_doctype` is declared AND native AND not for a reader.
			native = {f for f in _names(_native(cmd)(TASK)) if not self._hideable(TASK, f)}
			expected = (self.declared & native) | self.derived
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

	def _hideable(self, doctype, fieldname):
		"""Whether the DOCTYPE says this column is not for a reader — the only reason a field may vanish."""
		df = frappe.get_meta(doctype).get_field(fieldname)
		return fieldname in task_lenses._ANNOTATIONS or bool(df and (df.hidden or df.report_hide))

	def test_a_field_vanishes_only_when_the_FIELD_says_so(self):
		"""The one reason a column may be missing. Every native entry is still offered unless the field
		itself carries `hidden` or `report_hide`, or it is one of frappe's annotation columns.

		This replaces `no other doctype is narrowed`, which said the layer touches CRM Task alone. It reads
		every doctype now — that is the point — so the guarantee moves from WHICH doctype to WHY a field
		went. A field dropped for any other reason is a defect, and a hard-coded list of names anywhere
		would put this red on the first doctype it did not cover."""
		for doctype in (TASK, *OTHER_DOCTYPES):
			for cmd in NATIVE_LENSES:
				declared = task_lenses.declared_fields(doctype)
				offered = _names(_dispatched(cmd)(doctype))
				for row in _native(cmd)(doctype):
					fieldname = row.get("fieldname")
					if self._hideable(doctype, fieldname):
						with self.subTest(doctype=doctype, cmd=cmd, gone=fieldname):
							self.assertNotIn(fieldname, offered,
							                 f"{fieldname} is marked not-for-a-reader and is still offered")
						continue
					if declared is not None and fieldname not in declared:
						continue  # CRM Task's own declaration, asserted by the lens tests above
					with self.subTest(doctype=doctype, cmd=cmd, kept=fieldname):
						self.assertIn(fieldname, offered,
						              f"{cmd} dropped {fieldname} for {doctype} and no field asked it to")

	def test_every_menu_calls_a_column_the_SAME_thing(self):
		"""The defect this layer exists to end: one column, four menus, four names. Sort said "Owner" where
		Filter said "Created By"; the header said "Task ID" where every menu said "Name".

		Asserted ACROSS the menus rather than against a list of expected words — a wording anyone disagrees
		with is then one place to change, and this still goes red the moment two surfaces disagree."""
		for doctype in (TASK, *OTHER_DOCTYPES):
			by_field = {}
			for cmd in (*NATIVE_LENSES, COLUMN_LENS):
				for row in _dispatched(cmd)(doctype):
					by_field.setdefault(row.get("fieldname"), {})[cmd] = row.get("label")
			for fieldname, seen in by_field.items():
				with self.subTest(doctype=doctype, field=fieldname):
					self.assertEqual(len(set(seen.values())), 1,
					                 f"{doctype}.{fieldname} is called {sorted(set(seen.values()))} across its menus")

	def test_the_id_column_wears_the_doctype_s_own_word(self):
		"""`name` is not a field, so nothing can be hung on it and every menu invented a word. The word is
		the doctype's own, read off the list declaration beside its other column names."""
		for doctype in (TASK, *OTHER_DOCTYPES):
			word = task_lenses._id_label(doctype)
			with self.subTest(doctype):
				self.assertTrue(word, f"{doctype} declares no word for its own id")
			for cmd in NATIVE_LENSES:
				row = next((r for r in _dispatched(cmd)(doctype) if r.get("fieldname") == "name"), None)
				if row:
					with self.subTest(doctype=doctype, cmd=cmd):
						self.assertEqual(row.get("label"), word, f"{cmd} calls {doctype}'s id something else")

	def test_a_metadata_column_is_named_by_FRAPPE_and_not_by_us(self):
		"""created / updated / by whom carry no DocField, which is why CRM typed them into four lists that
		drifted. Frappe declares them once and its own export reads that, so asking it is what makes the
		screen and the downloaded file agree. Compared against frappe's answer, never a copy of it."""
		for doctype in (TASK, *OTHER_DOCTYPES):
			meta = frappe.get_meta(doctype)
			for cmd in NATIVE_LENSES:
				for row in _dispatched(cmd)(doctype):
					fieldname = row.get("fieldname")
					if fieldname == "name" or meta.get_field(fieldname):
						continue  # a real field names itself; the id has its own test
					expected = meta.get_label(fieldname)
					if expected == "No Label":
						continue  # a derived field, named by the declaration that invented it
					with self.subTest(doctype=doctype, cmd=cmd, field=fieldname):
						self.assertEqual(row.get("label"), _(expected),
						                 f"{doctype}.{fieldname} is not called what frappe calls it")

	def test_a_native_entry_is_only_ever_DESCRIBED_never_rewritten(self):
		"""A native entry keeps its own fieldtype and options. Only the keys this layer declares it may
		touch may differ — the control description, the relayed stamps, and the label, which has its own
		two tests above."""
		for doctype in OTHER_DOCTYPES:
			for cmd in NATIVE_LENSES:
				described = set(task_lenses._ASSIGN_CONTROL) if cmd == DESCRIBED_LENS else set()
				allowed = STAMPED_KEYS | described | {"label"}
				native = {r.get("fieldname"): r for r in _native(cmd)(doctype)}
				for row in _dispatched(cmd)(doctype):
					base = native.get(row.get("fieldname"))
					if base is None:
						continue
					changed = {k for k in set(row) | set(base) if row.get(k) != base.get(k)}
					with self.subTest(doctype=doctype, cmd=cmd, field=row.get("fieldname")):
						self.assertLessEqual(changed, allowed, f"{cmd} rewrote {row.get('fieldname')} for {doctype}")
						for key in changed & described:
							self.assertEqual(row.get(key), task_lenses._ASSIGN_CONTROL[key],
							                 f"{cmd} set {key} on {row.get('fieldname')} to something undeclared")

	def test_the_assign_column_is_described_wherever_a_control_will_read_it(self):
		"""The other direction, which a "nothing was rewritten" check cannot see: the description must
		actually BE there. Dropping `_assign_control` changes nothing away from native and would pass every
		test above, while quietly returning the Leads filter to a box you type an email into."""
		checked = 0
		for doctype in OTHER_DOCTYPES:
			row = next(
				(r for r in _dispatched(DESCRIBED_LENS)(doctype) if r.get("fieldname") == "_assign"),
				None,
			)
			if row is None:
				continue  # CRM Task declares its own `assigned_to` column and never offers frappe's
			checked += 1
			with self.subTest(doctype):
				for key, value in task_lenses._ASSIGN_CONTROL.items():
					self.assertEqual(row.get(key), value, f"_assign lost its declared {key} on {doctype}")
		self.assertTrue(checked, "no doctype offered _assign — this test proved nothing")

	def test_the_column_lens_answers_every_doctype(self):
		"""The add-column picker used to get an EMPTY answer for every doctype but CRM Task, and empty is
		ColumnSettings' own contract for "read doctype meta in the browser instead" — which is where "Owner"
		and "Last Modified" kept coming back from. It answers the same set as the filter lens now, so the
		picker and the filter menu cannot disagree about what exists or what it is called."""
		for doctype in (TASK, *OTHER_DOCTYPES):
			with self.subTest(doctype):
				self.assertEqual(
					_names(_dispatched(COLUMN_LENS)(doctype)),
					_names(_dispatched(NATIVE_LENSES[0])(doctype)),
					f"the column picker and the filter menu offer different fields for {doctype}",
				)
