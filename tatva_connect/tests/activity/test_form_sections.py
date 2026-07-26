# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Phase 8 of docs/plans/task-form-layer/2026-07-25-task-slots-to-sections-and-form-layer.md — the form has a layout.

`type_config` used to answer with one flat list of fields and nothing about how they are arranged. LSQ's
forms are tabs -> titled sections -> two-up fields (§14), and every one of those facts already has a home:
`CRM Task Section` carries `title`, `tab`, `display_order` and `depends_on`. So the layout is READ off the
declaration and returned beside the fields; no renderer decides it and no second table holds it.

What is asserted:

  * `type_config` returns `groups`, one per section the type's fields name, each carrying that section's
    own title / tab / display order / condition, with the fields in the child table's own order;
  * the union of the groups is EXACTLY the flat `fields` list — no field dropped, none duplicated, and
    the two are the same objects, so a type can never have two different answers to "what fields?";
  * the DEFAULT: a type no admin has sectioned yields ONE unsectioned, untitled group holding every
    field, in declaration order. That is today's flat form, which is why Phase 8 changes nothing on
    screen until someone assigns a section;
  * a hidden SECTION's required field does not block a save — enforced on the SERVER, through the same
    `_field_visible` evaluator a hidden FIELD already goes through, and proved against its own control
    (the same save with the section SHOWN must still be refused).

No section key, title or child table is named as a literal: the sections are read off their own rows, so
an operator renaming one (D13 says they may) moves this test with them.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.activity.test_form_sections
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.activity import api as activity_api
from tatva_connect.tests.activity import task_type_fixture

FLAT_TYPE = "ZZ Form Sections Flat Probe"
LAID_OUT_TYPE = "ZZ Form Sections Layout Probe"
GATED_TYPE = "ZZ Form Sections Gated Probe"

GATE = "zz_fs_gate"
SHOW_GATE = "eval:doc.zz_fs_gate=='Yes'"
ANSWER = "ZZ form sections answer"


def _column_sections():
	"""Two seeded sections that are plain column shapes — the ones a field is grouped under on the form.
	Read off the declaration and never named: the keys and titles are the operator's (D13)."""
	return frappe.get_all(
		"CRM Task Section",
		filters={"is_key_value": 0, "is_multi_row": 0},
		fields=["name", "title", "tab", "display_order", "depends_on", "target_doctype"],
		order_by="display_order",
	)[:2]


def _fieldnames(group):
	return [f["fieldname"] for f in group["fields"]]


class TestFormSections(FrappeTestCase):
	"""Three types: one nobody sectioned, one laid out across two sections, one behind a section gate."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.first, cls.second = _column_sections()

		# Declaration order is deliberately NOT section order: the unsectioned field is declared last and
		# must still lead, and the two sectioned fields interleave, so an implementation that merely kept
		# the child table's order would fail.
		cls.flat_type = task_type_fixture.mint_type(FLAT_TYPE, [
			{"label": "ZZ Flat One", "fieldname": "zz_fs_flat_one", "fieldtype": "Data"},
			{"label": "ZZ Flat Two", "fieldname": "zz_fs_flat_two", "fieldtype": "Data"},
		])
		cls.laid_out_type = task_type_fixture.mint_type(LAID_OUT_TYPE, [
			{"label": "ZZ B First", "fieldname": "zz_fs_b1", "fieldtype": "Data", "section": cls.second.name},
			{"label": "ZZ A First", "fieldname": "zz_fs_a1", "fieldtype": "Data", "section": cls.first.name},
			{"label": "ZZ B Second", "fieldname": "zz_fs_b2", "fieldtype": "Data", "section": cls.second.name},
			{"label": "ZZ A Second", "fieldname": "zz_fs_a2", "fieldtype": "Data", "section": cls.first.name},
			{"label": "ZZ Loose", "fieldname": "zz_fs_loose", "fieldtype": "Data"},
		])
		# The gate lives OUTSIDE the gated section, which is the shape a real form has: an answer in one
		# section decides whether another is asked at all.
		cls.gated_type = task_type_fixture.mint_type(GATED_TYPE, [
			{"label": "ZZ Gate", "fieldname": GATE, "fieldtype": "Data"},
			{"label": "ZZ Gated Answer", "fieldname": "zz_fs_gated", "fieldtype": "Data",
			 "section": cls.second.name, "reqd": 1},
		])

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		task_type_fixture.teardown()
		super().tearDownClass()

	def setUp(self):
		# Registered FIRST so it runs LAST (addCleanup is LIFO): a section row edited below is restored by
		# the rollback, and only then is the cached copy `_section_condition` reads through dropped.
		self.addCleanup(self._forget_sections)
		self.addCleanup(frappe.db.rollback)
		frappe.set_user("Administrator")
		self.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Form Sections Probe",
			"mobile_no": f"+9198126{int(frappe.generate_hash(length=8), 16) % 100000:05d}",
			"custom_vertical": task_type_fixture.VERTICAL, "custom_group": task_type_fixture.GROUP,
		}).insert(ignore_permissions=True)

	def _forget_sections(self):
		for section in (self.first, self.second):
			frappe.clear_document_cache("CRM Task Section", section.name)

	def _gate_the_section(self, condition):
		"""Put a condition on a real section row for the length of one test. The rollback restores it."""
		frappe.db.set_value("CRM Task Section", self.second.name, "depends_on", condition)
		frappe.clear_document_cache("CRM Task Section", self.second.name)

	# ---- the premise ---------------------------------------------------------------------------------

	def test_the_site_declares_two_distinct_column_sections(self):
		"""One section cannot show that a field lands under its OWN heading and not the other one's."""
		self.assertNotEqual(self.first.name, self.second.name,
							"only one column section is seeded — the layout is untestable")
		self.assertTrue(self.first.title and self.second.title,
						"a section without a title has no heading to render")
		self.assertEqual(self.first.tab or "", self.second.tab or "",
						 "the two probe sections sit in different tabs — the order asserted below is a "
						 "display order within one tab, so put them in one or pick two others")
		self.assertEqual((self.first.depends_on or "", self.second.depends_on or ""), ("", ""),
						 "a probe section already carries a condition — the required tests below gate it "
						 "themselves and would be judging the operator's condition, not theirs")

	# ---- the default: nothing changes on screen ------------------------------------------------------

	def test_a_type_nobody_sectioned_is_one_unsectioned_group_of_every_field(self):
		"""The whole reason Phase 8 is invisible on delivery. No live CRM Task Type Field carries a section
		today, so every type takes this branch and renders the flat two-up form it already renders."""
		cfg = activity_api._type_config(self.flat_type)
		self.assertEqual(len(cfg["groups"]), 1, "a type with no sections was split into several groups")
		group = cfg["groups"][0]
		self.assertEqual((group["section"], group["title"], group["tab"], group["depends_on"]),
						 ("", "", "", ""), "the unsectioned group grew a heading, a tab or a condition")
		self.assertEqual(_fieldnames(group), [f["fieldname"] for f in cfg["fields"]],
						 "the unsectioned group is not the declaration, in the declaration's own order")

	# ---- the layout ----------------------------------------------------------------------------------

	def test_fields_are_grouped_by_the_section_they_declare(self):
		"""One group per named section, holding exactly the fields that name it, in child-table order."""
		groups = {g["section"]: g for g in activity_api._type_config(self.laid_out_type)["groups"]}
		self.assertEqual(set(groups), {"", self.first.name, self.second.name},
						 "the groups are not one per declared section plus the unsectioned one")
		self.assertEqual(_fieldnames(groups[self.first.name]), ["zz_fs_a1", "zz_fs_a2"])
		self.assertEqual(_fieldnames(groups[self.second.name]), ["zz_fs_b1", "zz_fs_b2"])
		self.assertEqual(_fieldnames(groups[""]), ["zz_fs_loose"])

	def test_each_group_carries_its_own_sections_declared_presentation(self):
		"""Title, tab, order and condition are the section row's — restated nowhere, so an operator
		renaming a section renames its heading and nothing else has to be touched."""
		groups = {g["section"]: g for g in activity_api._type_config(self.laid_out_type)["groups"]}
		for section in (self.first, self.second):
			g = groups[section.name]
			self.assertEqual(
				(g["title"], g["tab"], g["display_order"], g["depends_on"]),
				(section.title, section.tab or "", int(section.display_order or 0), section.depends_on or ""),
				f"the group for `{section.name}` does not carry its section's own presentation",
			)

	def test_the_unsectioned_group_leads_and_the_sections_follow_in_display_order(self):
		"""Order is the declaration's: unsectioned first, then sections by display_order within the tab."""
		order = [g["section"] for g in activity_api._type_config(self.laid_out_type)["groups"]]
		self.assertEqual(order, ["", self.first.name, self.second.name],
						 "the groups came back in an order the declaration did not ask for")

	def test_the_groups_are_exactly_the_flat_field_list(self):
		"""A layout that could drop or duplicate a field would be a second answer to 'what fields?'."""
		cfg = activity_api._type_config(self.laid_out_type)
		grouped = [f for g in cfg["groups"] for f in g["fields"]]
		self.assertEqual(sorted(f["fieldname"] for f in grouped),
						 sorted(f["fieldname"] for f in cfg["fields"]),
						 "the layout holds a different set of fields from the flat list")
		self.assertEqual(len(grouped), len(cfg["fields"]), "a field was placed in more than one group")

	# ---- a hidden section's required field must not block a save (server side) -------------------------

	def test_a_required_field_in_a_hidden_section_does_not_block_the_save(self):
		"""The rule Phase 8 adds, on the SERVER — Vue hiding it is not enforcement. §8: a section whose
		condition is false is hidden and its fields stop being required, exactly as api.py already does per
		field. RED before the change: the save is refused for a field the rep was never shown."""
		self._gate_the_section(SHOW_GATE)

		name = activity_api.save_activity(self.lead.name, self.gated_type, {GATE: "No"})

		self.assertTrue(frappe.db.exists("CRM Task", name),
						"the save was refused for a field inside a section the form never showed")

	def test_the_same_field_is_still_required_when_its_section_is_shown(self):
		"""The control. Without it the test above would pass on a build that simply stopped enforcing reqd."""
		self._gate_the_section(SHOW_GATE)

		with self.assertRaises(frappe.ValidationError) as caught:
			activity_api.save_activity(self.lead.name, self.gated_type, {GATE: "Yes"})
		self.assertIn("ZZ Gated Answer", str(caught.exception),
					  "the save was refused by something other than the missing required field")

	def test_the_field_is_required_when_its_section_carries_no_condition(self):
		"""And the baseline: an ungated section changes nothing about required — this phase adds a way to
		suspend the rule, never a way to lose it."""
		with self.assertRaises(frappe.ValidationError) as caught:
			activity_api.save_activity(self.lead.name, self.gated_type, {GATE: "No"})
		self.assertIn("ZZ Gated Answer", str(caught.exception),
					  "a required field in an unconditional section stopped being required")

	def test_the_answer_still_lands_when_the_gated_section_is_shown_and_filled(self):
		"""Hiding a section suspends the requirement; it never changes where the answer goes."""
		self._gate_the_section(SHOW_GATE)

		name = activity_api.save_activity(
			self.lead.name, self.gated_type, {GATE: "Yes", "zz_fs_gated": ANSWER})

		cfg = activity_api._type_config(self.gated_type)
		values = activity_api._task_values(frappe.get_doc("CRM Task", name), cfg)
		self.assertEqual(values.get("zz_fs_gated"), ANSWER,
						 "the answer to a gated section's field did not reach its home")
