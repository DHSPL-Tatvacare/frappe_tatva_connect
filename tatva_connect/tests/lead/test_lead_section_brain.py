# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A section's facts live on `CRM Lead Section`, once, and every reader asks it.

A section answers four questions: which table holds these fields, whose columns they are, may a lead
carry many of these rows, and which column addresses one. Those four were restated on all 607 catalog
rows — and rotted exactly as a copy does. `sql_source` was added to a populated table and 29 rows were
born blank, so a field the API accepts can never appear in a Smart View. `is_row_key` was set on ONE
row of 607, so the brain called Lab single-row: a coach editing report 5 overwrote report 1.

Seven rows replace 607 copies, two Python dicts and a doctype Select. `mx` — LeadSquared's custom-field
prefix — is named for what it is, re-keyed through `rename_doc` so the Links cascade instead of
orphaning (the mistake that made `applies_to` unreachable on 397 rows).

4.9 is the regression lock: the catalog's SHAPE moved, what a key resolves to did not. If it goes red
the resolution changed — stop, and never adjust it to fit.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.api import partner
from tatva_connect.api._base import ACTION_CREATED, ACTION_UPDATED
from tatva_connect.lead_sync import contract
from tatva_connect.partner_api import section_seed
from tatva_connect.partner_api.doctype.crm_lead_section import crm_lead_section
from tatva_connect.patches import rekey_lead_catalog_sections
from tatva_connect.tests.api import partner_fixture

DT = "CRM Lead Section"
CATALOG = "CRM Lead API Field"

PARTNER = "section.brain.partner@example.test"
PHONE = "+916100010001"  # a distinctive range this module owns outright, purged in tearDownClass

# The end state, declared here — never read back from the seeder, or this would test itself. This
# structure is OURS (partner_api/section_seed.py declares it); the field rows that name a section are
# the operator's, so no count of them appears anywhere below.
# section_key -> (title, display_order, child_table_field, target_doctype, is_multi_row, row_key_field)
SECTIONS = {
	"lead":    ("Lead Details",     10, "",                              "CRM Lead",                   0, ""),
	"acq":     ("Acquisition",      20, "custom_acquisition_profile",    "CRM Acquisition Profile",    0, ""),
	"plan":    ("Plan",             30, "custom_plan_profile",           "CRM Plan Profile",           0, ""),
	"lab":     ("Lab",              40, "custom_lab_profile",            "CRM Lab Profile",            1, "report_date"),
	"care":    ("Care & Providers", 60, "custom_care_providers_profile", "CRM Care Providers Profile", 0, ""),
	"drug":    ("Drug Program",     70, "custom_drug_program_profile",   "CRM Drug Program Profile",   1, "cycle_date"),
	"metrics": ("Activity Metrics", 80, "custom_lead_activity_metrics",  "CRM Lead Activity Metrics",  0, ""),
}


def _purge_test_leads():
	for name in frappe.get_all("CRM Lead", filters={"mobile_no": ["like", "+91610001%"]}, pluck="name"):
		frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)


class TestLeadSectionBrain(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		partner_fixture.mint_partner(PARTNER)  # empty grid -> the whole catalog, so every section is in play
		_purge_test_leads()
		frappe.db.commit()  # survives the per-test rollback; the upsert walk resolves the contract live

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		_purge_test_leads()  # before the fixture: the leads hang off the grain it is about to drop
		partner_fixture.teardown()
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		self.sp = f"lead_section_{frappe.generate_hash(length=6)}"
		frappe.db.savepoint(self.sp)
		frappe.cache().delete_value(partner._CATALOG_CACHE_KEY)

	def tearDown(self):
		frappe.set_user("Administrator")
		try:
			frappe.db.rollback(save_point=self.sp)
		except Exception:
			frappe.db.rollback()  # the patch-replay test commits, which discards the savepoint
		frappe.cache().delete_value(partner._CATALOG_CACHE_KEY)

	# -- helpers -------------------------------------------------------------

	def _draft(self, **kw):
		"""An unsaved section carrying only what a validate rule needs to judge it."""
		return frappe.get_doc({
			"doctype": DT, "section_key": f"zz{frappe.generate_hash(length=6)}",
			"title": "Draft", "display_order": 999, **kw,
		})

	def _upsert(self, phone):
		"""The real walk for this caller: allowed keys -> _split_keys -> _collect -> _upsert_one."""
		frappe.set_user(PARTNER)
		u, mp, is_sysmgr, parent_fields, child_allow = partner._caller_fields()
		programs = partner._allowed_programs(u, bool(mp))
		item = frappe._dict({"mobile_no": phone, "first_name": "Section Brain"})
		return partner._upsert_one(item, mp, is_sysmgr, parent_fields, child_allow, programs)

	def _fingerprint(self):
		return (
			frappe.get_all(DT, fields=["name", "child_table_field", "is_multi_row", "row_key_field"],
			               order_by="name asc"),
			frappe.get_all(CATALOG, fields=["name", "section"], order_by="name asc"),
		)

	# -- the seven rows ------------------------------------------------------

	def test_4_1_the_seven_sections_are_the_declared_end_state(self):
		"""Exactly seven. `clinical` is not among them: it never had a lead field, only two CRM Task rows."""
		self.assertEqual(set(frappe.get_all(DT, pluck="name")), set(SECTIONS))
		for key, want in SECTIONS.items():
			doc = frappe.get_doc(DT, key)
			got = (doc.title, doc.display_order, doc.child_table_field or "",
			       doc.target_doctype, doc.is_multi_row, doc.row_key_field or "")
			self.assertEqual(got, want, f"section {key}")

	def test_4_2_lab_is_multi_row_keyed_on_report_date(self):
		"""THE CORRECTION. `lab:report_date` carried is_row_key=0, so the brain called Lab single-row
		and every write merged onto row 0 — report 5 overwrote report 1, and no Lab value could trend."""
		self.assertEqual(partner._child_key_field("custom_lab_profile"), "report_date")
		sec = frappe.get_doc(DT, "lab")
		self.assertTrue(sec.is_multi_row)
		self.assertEqual(sec.row_key_field, "report_date")

	def test_4_3_drug_is_multi_row_keyed_on_cycle_date(self):
		self.assertEqual(partner._child_key_field("custom_drug_program_profile"), "cycle_date")
		sec = frappe.get_doc(DT, "drug")
		self.assertTrue(sec.is_multi_row)
		self.assertEqual(sec.row_key_field, "cycle_date")

	# -- the catalog points at them ------------------------------------------

	def test_4_4_every_lead_catalog_rows_section_link_resolves(self):
		"""A Link, so a re-key cascades. `applies_to` held its key as text and 397 rows were orphaned."""
		sections = set(frappe.get_all(DT, pluck="name"))
		rows = frappe.get_all(CATALOG, fields=["name", "section"])
		dangling = sorted(r.name for r in rows if r.section and r.section not in sections)
		self.assertEqual(dangling, [], "catalog rows whose section Link points at nothing")
		# Every row: the 397 CRM Task rows that had no lead section are gone from this table, so there
		# is no longer a row in it that is not a lead field.
		unlinked = sorted(r.name for r in rows if not r.section)
		self.assertEqual(unlinked, [], "lead catalog rows with no section")

	def test_4_5_sql_source_is_a_pure_function_of_the_section_so_no_row_can_be_born_blank(self):
		"""THE CORRECTION. `sql_source` was a column on all 607 catalog rows; it was added to a populated
		table on 2026-06-20 and the seed that filled it was written nine days earlier, so 29 keys were
		never revisited and were born blank. Derived from the section, a blank is unrepresentable — the
		answer is computed, so there is no row left to forget. Two sections, both answers, no counting."""
		self.assertEqual(crm_lead_section.sql_source(frappe.get_doc(DT, "lead")), "parent")
		self.assertEqual(crm_lead_section.sql_source(frappe.get_doc(DT, "lab")), "child")

	# -- validate ------------------------------------------------------------

	def test_4_6_validate_refuses_multi_row_with_no_row_key(self):
		"""Multi-row means a row has an address. Without one there is no upsert, only a clobber."""
		doc = self._draft(target_doctype="CRM Lab Profile", child_table_field="custom_lab_profile",
		                  is_multi_row=1)
		self.assertRaises(frappe.ValidationError, doc.insert)

	def test_4_7_validate_refuses_a_row_key_that_is_not_a_column(self):
		doc = self._draft(target_doctype="CRM Lab Profile", child_table_field="custom_lab_profile",
		                  is_multi_row=1, row_key_field="not_a_column")
		self.assertRaises(frappe.ValidationError, doc.insert)

	def test_4_8_validate_refuses_a_child_table_that_does_not_reach_the_target(self):
		doc = self._draft(target_doctype="CRM Lab Profile", child_table_field="custom_plan_profile")
		self.assertRaises(frappe.ValidationError, doc.insert)
		# Blank child table means the lead row itself, so no other target is reachable.
		doc = self._draft(target_doctype="CRM Lab Profile")
		self.assertRaises(frappe.ValidationError, doc.insert)

	# -- nothing a partner sees moved ---------------------------------------

	def test_4_9_a_key_is_routed_to_the_table_its_section_names(self):
		"""THE LOCK, and it needs no partner at all. What a key resolves to is the section's answer:
		`lead:` is the lead row, `lab:` and `drug:` are the tables those sections name. It used to be
		asserted as three per-partner key COUNTS read off the dev site — which move when an operator
		ticks a box, and never said which table a key reached. Never fix this by adjusting it: if it is
		red the resolution changed and the phase is wrong."""
		parent_fields, child_allow = partner._split_keys(["lead:mobile_no", "lab:hba1c", "drug:dose"])
		self.assertEqual(parent_fields, ["mobile_no"])
		self.assertEqual(child_allow["custom_lab_profile"], ["hba1c"])
		self.assertEqual(child_allow["custom_drug_program_profile"], ["dose"])

	def test_4_10_a_real_partner_create_still_creates_then_dedupes(self):
		"""End to end on a minted contract: the section refactor moved the shape under the write path,
		so the walk it feeds must still create once and fold every re-send onto that same lead."""
		doc, action = self._upsert(PHONE)
		self.assertEqual(action, ACTION_CREATED)
		again, action = self._upsert(PHONE)
		self.assertEqual(action, ACTION_UPDATED)
		self.assertEqual(again.name, doc.name, "a re-send is that lead, never a second")

	def test_4_11_the_facebook_fold_still_lands_utm_campaign_in_the_acquisition_profile(self):
		item = {}
		contract.stage(item, "acq:utm_campaign", "spring-2026")
		self.assertEqual(item, {"custom_acquisition_profile": [{"utm_campaign": "spring-2026"}]})

	# -- the rename landed ---------------------------------------------------

	def test_4_13_mx_is_gone_and_the_section_is_named_for_what_it_is(self):
		"""`mx_` was LeadSquared's custom-field prefix. It named the source, never the fact."""
		self.assertEqual(frappe.get_all(CATALOG, filters={"field_key": ["like", "mx:%"]}, pluck="name"), [])
		self.assertFalse(frappe.db.exists(DT, "mx"))

	def test_4_15_the_patch_declares_an_end_state_and_replays_clean(self):
		"""Idempotent: it asserts reality rather than assuming what ran before."""
		before = self._fingerprint()
		rekey_lead_catalog_sections.execute()
		rekey_lead_catalog_sections.execute()
		self.assertEqual(self._fingerprint(), before)

	def test_the_seeder_is_the_one_home_of_the_seven_rows(self):
		"""after_migrate and the patch call the SAME function, so they can never declare two end states."""
		section_seed.ensure_rows()
		self.assertEqual(set(frappe.get_all(DT, pluck="name")), set(SECTIONS))
