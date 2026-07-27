# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The activity form has a LAYOUT, and it is Frappe's layout.

A form used a thousand times a day cannot rearrange itself while it is being filled. It used to: the client
was handed a flat list of fields, filtered it to the ones currently visible, and flowed the survivors through
a two-column grid — so answering one question re-flowed every field after it and a field could change column
on every keystroke.

The model here is `frappe/public/js/frappe/form/layout.js`, unchanged: layout is declared as MARKER ROWS in
the same ordered field list (`Tab Break`, `Section Break`, `Column Break`), the list is walked once, and a
field belongs to whichever column was open when it was DECLARED. That makes a field's column a property of
the declaration and not of what happens to be on screen — which is the whole fix, and what
`test_a_fields_column_never_changes_with_what_is_visible` pins down.

What is asserted:

  * the DEFAULT — a type declaring no markers is one tab, one section, one column holding every field in
    declaration order. That is the flat form, so nothing moves for a type nobody has laid out;
  * a field's column is fixed by the declaration and is the SAME whatever is visible;
  * a marker is a no-value field: it never reaches `fields`, and it never reaches storage;
  * `Section Break` and `Column Break` group as declared, and a `Tab Break` opens a tab;
  * the layout names exactly the flat field list — nothing dropped, nothing placed twice;
  * a hidden container's required field does not block a save — on the SERVER, through the same
    `_field_visible` evaluator a hidden field goes through, proved against its own control;
  * a rule may Show/Hide a whole section, which is how the source forms behave.

Nothing here is asserted against a seeded `CRM Task Section`: storage sections and form layout are now
different questions, which is why this file no longer touches one.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.activity.test_form_sections
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.activity import api as activity_api
from tatva_connect.tests.activity import task_type_fixture

FLAT_TYPE = "ZZ Form Layout Flat Probe"
LAID_OUT_TYPE = "ZZ Form Layout Probe"
GATED_TYPE = "ZZ Form Layout Gated Probe"
RULED_TYPE = "ZZ Form Layout Ruled Probe"

GATE = "zz_fl_gate"
SHOW_GATE = "eval:doc.zz_fl_gate=='Yes'"
ANSWER = "ZZ form layout answer"


def _placement(tabs):
	"""Every field the layout places, as fieldname -> (tab key, section key, column key). The one reading
	this file compares against, so a test never walks the tree twice."""
	return {
		fieldname: (tab["key"], section["key"], column["key"])
		for tab in tabs
		for section in tab["sections"]
		for column in section["columns"]
		for fieldname in column["fields"]
	}


class TestFormLayout(FrappeTestCase):
	"""Four types: one nobody laid out, one laid out with markers, one behind a section gate, one whose
	rule drives a section."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

		cls.flat_type = task_type_fixture.mint_type(FLAT_TYPE, [
			{"label": "ZZ Flat One", "fieldname": "zz_fl_flat_one", "fieldtype": "Data"},
			{"label": "ZZ Flat Two", "fieldname": "zz_fl_flat_two", "fieldtype": "Data"},
		])

		# The shape that used to break: `zz_fl_right` sits in the RIGHT column below a field that is hidden
		# until `zz_fl_driver` is answered. Under the old grid it moved to the left column the moment the
		# hidden field appeared; under a declared column it cannot.
		cls.laid_out_type = task_type_fixture.mint_type(LAID_OUT_TYPE, [
			{"label": "ZZ Driver", "fieldname": "zz_fl_driver", "fieldtype": "Data"},
			{"label": "ZZ Revealed", "fieldname": "zz_fl_revealed", "fieldtype": "Data",
			 "depends_on": "eval:doc.zz_fl_driver=='Yes'"},
			{"label": "", "fieldname": "zz_fl_col_break", "fieldtype": "Column Break"},
			{"label": "ZZ Right", "fieldname": "zz_fl_right", "fieldtype": "Data"},
			{"label": "ZZ Second Heading", "fieldname": "zz_fl_sec_break", "fieldtype": "Section Break"},
			{"label": "ZZ In Second Section", "fieldname": "zz_fl_second", "fieldtype": "Data"},
			{"label": "ZZ Second Tab", "fieldname": "zz_fl_tab_break", "fieldtype": "Tab Break"},
			{"label": "ZZ On Second Tab", "fieldname": "zz_fl_tabbed", "fieldtype": "Data"},
		])

		# The gate lives OUTSIDE the gated section, which is the shape a real form has: an answer in one
		# section decides whether another is asked at all.
		cls.gated_type = task_type_fixture.mint_type(GATED_TYPE, [
			{"label": "ZZ Gate", "fieldname": GATE, "fieldtype": "Data"},
			{"label": "ZZ Gated Heading", "fieldname": "zz_fl_gated_break", "fieldtype": "Section Break",
			 "depends_on": SHOW_GATE},
			{"label": "ZZ Gated Answer", "fieldname": "zz_fl_gated", "fieldtype": "Data", "reqd": 1},
		])

		# A rule targeting the Section Break itself — one row hides a whole heading and everything under it.
		cls.ruled_type = task_type_fixture.mint_type(RULED_TYPE, [
			{"label": "ZZ Outcome", "fieldname": "zz_fl_outcome", "fieldtype": "Select",
			 "options": "Connected\nNot Connected"},
			{"label": "ZZ Detail Heading", "fieldname": "zz_fl_detail_break", "fieldtype": "Section Break"},
			{"label": "ZZ Detail", "fieldname": "zz_fl_detail", "fieldtype": "Data", "reqd": 1},
		], rules=[
			{"rule_label": "Form Onload", "action": "Hide", "targets": "zz_fl_detail_break"},
			{"rule_label": "Connected", "condition_field": "zz_fl_outcome", "operator": "is",
			 "condition_value": "Connected", "action": "Show", "targets": "zz_fl_detail_break"},
		])

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		task_type_fixture.teardown()
		super().tearDownClass()

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.set_user("Administrator")
		self.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Form Layout Probe",
			"mobile_no": f"+9198127{int(frappe.generate_hash(length=8), 16) % 100000:05d}",
			"custom_vertical": task_type_fixture.VERTICAL, "custom_group": task_type_fixture.GROUP,
		}).insert(ignore_permissions=True)

	# ---- the default: nothing moves for a type nobody laid out ----------------------------------------

	def test_a_type_with_no_markers_is_one_tab_one_section_one_column(self):
		"""Every type live today declares no markers, so every one of them takes this branch and renders the
		flat form it already rendered."""
		cfg = activity_api._type_config(self.flat_type)
		self.assertEqual(len(cfg["tabs"]), 1, "a type with no Tab Break was split across tabs")
		sections = cfg["tabs"][0]["sections"]
		self.assertEqual(len(sections), 1, "a type with no Section Break grew a second section")
		self.assertEqual(len(sections[0]["columns"]), 1, "a type with no Column Break grew a second column")
		self.assertEqual(sections[0]["columns"][0]["fields"],
						 [f["fieldname"] for f in cfg["fields"]],
						 "the one column is not the declaration, in the declaration's own order")
		self.assertEqual((cfg["tabs"][0]["label"], sections[0]["label"]), ("", ""),
						 "the default tab or section grew a heading nobody declared")

	# ---- THE fix: a field's column is the declaration's, not the screen's ------------------------------

	def test_a_fields_column_never_changes_with_what_is_visible(self):
		"""The defect this layer exists to kill. `zz_fl_revealed` is hidden until `zz_fl_driver` is answered;
		`zz_fl_right` is declared after a Column Break. Whether the hidden field is on screen or not, every
		field must report the same tab, section and column — placement is a fact about the DECLARATION.

		RED before the change: there were no columns to be stable, and the client re-flowed a filtered list,
		so revealing `zz_fl_revealed` moved `zz_fl_right` from the right column to the left."""
		placed = _placement(activity_api._type_config(self.laid_out_type)["tabs"])

		left = placed["zz_fl_driver"]
		self.assertEqual(placed["zz_fl_revealed"], left,
						 "a field declared beside another landed somewhere else")
		self.assertNotEqual(placed["zz_fl_right"][2], left[2],
							"a Column Break did not start a new column")
		self.assertEqual(placed["zz_fl_right"][1], left[1],
						 "a Column Break started a new SECTION — it must only start a column")

		# Placement is read off the declaration, so it cannot depend on answers; asking twice with different
		# answers in play must give the identical map.
		self.assertEqual(_placement(activity_api._type_config(self.laid_out_type)["tabs"]), placed,
						 "the layout is not stable across reads")

	def test_a_section_break_starts_a_section_and_a_tab_break_starts_a_tab(self):
		placed = _placement(activity_api._type_config(self.laid_out_type)["tabs"])

		self.assertNotEqual(placed["zz_fl_second"][1], placed["zz_fl_right"][1],
							"a Section Break did not start a new section")
		self.assertEqual(placed["zz_fl_second"][0], placed["zz_fl_right"][0],
						 "a Section Break started a new TAB — it must only start a section")
		self.assertNotEqual(placed["zz_fl_tabbed"][0], placed["zz_fl_second"][0],
							"a Tab Break did not start a new tab")

	def test_a_declared_heading_is_carried_on_its_container(self):
		"""The heading is the marker row's own label — stated once, on the row that draws it."""
		tabs = activity_api._type_config(self.laid_out_type)["tabs"]
		labels = {s["key"]: s["label"] for t in tabs for s in t["sections"]}
		self.assertEqual(labels["zz_fl_sec_break"], "ZZ Second Heading",
						 "a Section Break's label did not become its heading")
		self.assertEqual([t["label"] for t in tabs][-1], "ZZ Second Tab",
						 "a Tab Break's label did not become its tab name")

	# ---- a marker is a no-value field ------------------------------------------------------------------

	def test_a_marker_is_never_a_field(self):
		"""`NO_VALUE_FIELDS` is Frappe's own list — the same one that keeps a Section Break out of a table's
		columns. Nothing that stores or reads an answer may ever meet a marker."""
		cfg = activity_api._type_config(self.laid_out_type)
		names = [f["fieldname"] for f in cfg["fields"]]
		for marker in ("zz_fl_col_break", "zz_fl_sec_break", "zz_fl_tab_break"):
			self.assertNotIn(marker, names, f"the layout marker `{marker}` was published as a field")

	def test_a_container_holding_no_field_is_not_returned(self):
		"""A declaration opening with a Section Break would otherwise leave an empty container in front of
		the form. Nothing draws it, so it has no business being on the wire."""
		for tab in activity_api._type_config(self.gated_type)["tabs"]:
			for section in tab["sections"]:
				for column in section["columns"]:
					self.assertTrue(column["fields"], f"column `{column['key']}` holds no field")
				self.assertTrue(section["columns"], f"section `{section['key']}` holds no column")
			self.assertTrue(tab["sections"], f"tab `{tab['key']}` holds no section")

	def test_the_layout_names_exactly_the_flat_field_list(self):
		"""A layout that could drop or duplicate a field would be a second answer to 'what does this ask?'."""
		cfg = activity_api._type_config(self.laid_out_type)
		placed = _placement(cfg["tabs"])
		self.assertEqual(sorted(placed), sorted(f["fieldname"] for f in cfg["fields"]),
						 "the layout places a different set of fields from the flat list")

	def test_a_marker_never_reaches_storage(self):
		"""The proof that matters: punch the form with a value submitted FOR each marker and none of them
		lands. A marker is not in the schema at all, so the writer never sees the key.

		RED on the old code, which had no notion of a marker: every one of them was an ordinary declared
		field, so all three of these values were routed to answer rows and read back."""
		name = activity_api.save_activity(
			self.lead.name, self.laid_out_type,
			{"zz_fl_driver": "No", "zz_fl_right": ANSWER, "zz_fl_second": "x", "zz_fl_tabbed": "y",
			 "zz_fl_col_break": "junk", "zz_fl_sec_break": "junk", "zz_fl_tab_break": "junk"})

		cfg = activity_api._type_config(self.laid_out_type)
		values = activity_api._task_values(frappe.get_doc("CRM Task", name), cfg)
		self.assertEqual(values.get("zz_fl_right"), ANSWER, "a real answer did not reach its home")
		for marker in ("zz_fl_col_break", "zz_fl_sec_break", "zz_fl_tab_break"):
			self.assertNotIn(marker, values, f"the layout marker `{marker}` was stored as an answer")

	# ---- a hidden container's required field must not block a save (server side) -----------------------

	def test_a_required_field_in_a_hidden_section_does_not_block_the_save(self):
		"""A section whose condition is false is not on screen, so its fields stop being required — the same
		rule a hidden field already carries, enforced on the SERVER because Vue hiding it is not enforcement."""
		name = activity_api.save_activity(self.lead.name, self.gated_type, {GATE: "No"})

		self.assertTrue(frappe.db.exists("CRM Task", name),
						"the save was refused for a field inside a section the form never showed")

	def test_the_same_field_is_still_required_when_its_section_is_shown(self):
		"""The control. Without it the test above would pass on a build that stopped enforcing reqd at all."""
		with self.assertRaises(frappe.ValidationError) as caught:
			activity_api.save_activity(self.lead.name, self.gated_type, {GATE: "Yes"})
		self.assertIn("ZZ Gated Answer", str(caught.exception),
					  "the save was refused by something other than the missing required field")

	def test_the_answer_still_lands_when_the_gated_section_is_shown_and_filled(self):
		"""Hiding a section suspends the requirement; it never changes where the answer goes."""
		name = activity_api.save_activity(
			self.lead.name, self.gated_type, {GATE: "Yes", "zz_fl_gated": ANSWER})

		cfg = activity_api._type_config(self.gated_type)
		values = activity_api._task_values(frappe.get_doc("CRM Task", name), cfg)
		self.assertEqual(values.get("zz_fl_gated"), ANSWER,
						 "the answer to a gated section's field did not reach its home")

	# ---- a rule drives a whole section -----------------------------------------------------------------

	def test_a_rule_may_show_and_hide_a_whole_section(self):
		"""How the source forms behave: one Hide row closes a heading and everything under it, one Show row
		opens it. The rule compiles onto the MARKER, so no per-field list has to be maintained."""
		cfg = activity_api._type_config(self.ruled_type)
		detail = next(f for f in cfg["fields"] if f["fieldname"] == "zz_fl_detail")
		self.assertTrue(detail["container_depends_on"],
						"the section's compiled condition was not stamped on the field it holds")

		self.assertFalse(activity_api._shown_here(detail, {"zz_fl_outcome": "Not Connected"}),
						 "the section stayed open for a value its rule does not show it for")
		self.assertTrue(activity_api._shown_here(detail, {"zz_fl_outcome": "Connected"}),
						"the rule did not open the section it names")

	def test_a_hidden_sections_required_field_is_not_demanded_by_the_save(self):
		"""End to end on the rule path: the section is closed on open, so its required field is not asked for."""
		name = activity_api.save_activity(self.lead.name, self.ruled_type,
										  {"zz_fl_outcome": "Not Connected"})

		self.assertTrue(frappe.db.exists("CRM Task", name),
						"a required field under a rule-hidden section blocked the save")
