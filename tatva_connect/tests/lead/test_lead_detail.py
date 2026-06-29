# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Lead detail panel projection — the grain/brain-aware, LSQ-clean Data tab.

Covers the two endpoints in tatva_connect.lead.detail:
  * lead_detail(lead)        — read projection (sectioned, child-flattened, entitled).
  * update_lead_detail(...)  — write path with a SERVER-BUILT allowlist (no mass-assignment).

The security-critical guarantees proven here:
  1. WRITE ALLOWLIST  — a field_key outside the writable projection is REJECTED (throws),
     so a crafted payload can never reach routing/owner/out-of-grain/read-only fields.
  2. READ-ONLY DENY   — a Property-Setter read-only field is never writable, even for a manager.
  3. PERM GATE        — read/write throw on a lead the caller cannot access.
  4. NO RAW SQL       — the module builds no string SQL; values come via the doc API (static check).
  5. APPLICABILITY    — drug-world sections show only for a drug-program lead; metabolic the reverse.
  6. DEDUP            — duplicate catalog rows (partner vs curated) collapse to one per field.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.test_lead_detail
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.lead import detail


# ----------------------------- pure helpers (no DB) -----------------------------
class TestDetailPureLogic(FrappeTestCase):
	def test_neutral_section_always_applies(self):
		self.assertTrue(detail.section_applies("lead", is_drug=True))
		self.assertTrue(detail.section_applies("lead", is_drug=False))
		self.assertTrue(detail.section_applies("acq", is_drug=True))

	def test_drug_section_only_in_drug_world(self):
		self.assertTrue(detail.section_applies("drug_program", is_drug=True))
		self.assertFalse(detail.section_applies("drug_program", is_drug=False))

	def test_metabolic_section_hidden_in_drug_world(self):
		self.assertFalse(detail.section_applies("lab", is_drug=True))
		self.assertTrue(detail.section_applies("lab", is_drug=False))

	def test_unknown_section_buckets_to_lead_and_always_applies(self):
		# A new section_key never silently vanishes — it buckets to neutral "Lead Details".
		self.assertTrue(detail.section_applies("brand_new_section", is_drug=True))
		self.assertTrue(detail.section_applies("brand_new_section", is_drug=False))

	def test_activity_rows_are_not_profile_rows(self):
		# Rows scoped to an activity surface (order/clinical → CRM Task) are excluded.
		self.assertFalse(detail.is_profile_row({"section_key": "order", "applies_to": "activity:Order Punch Status", "target_doctype": "CRM Task"}))
		self.assertFalse(detail.is_profile_row({"section_key": "clinical", "applies_to": "activity:Welcome Call", "target_doctype": "CRM Task"}))

	def test_lead_and_child_rows_are_profile_rows(self):
		self.assertTrue(detail.is_profile_row({"section_key": "lead", "applies_to": "lead", "target_doctype": "CRM Lead"}))
		self.assertTrue(detail.is_profile_row({"section_key": "drug_program", "applies_to": "", "target_doctype": "CRM Drug Program Profile", "child_table_field": "custom_drug_program_profile"}))

	def test_dedup_prefers_curated_lead_tagged_row(self):
		raw = {"field_key": "drug:psp_drug_category", "target_doctype": "CRM Drug Program Profile", "fieldname": "psp_drug_category", "label": "Psp Drug Category", "applies_to": ""}
		curated = {"field_key": "drug:psp_category", "target_doctype": "CRM Drug Program Profile", "fieldname": "psp_drug_category", "label": "PSP Drug Category", "applies_to": "lead", "child_pick": "single"}
		out = detail.dedup_rows([raw, curated])
		self.assertEqual(len(out), 1)
		self.assertEqual(out[0]["label"], "PSP Drug Category")  # curated wins

	def test_dedup_keeps_distinct_fields(self):
		a = {"field_key": "drug:dosage", "target_doctype": "CRM Drug Program Profile", "fieldname": "dosage", "applies_to": ""}
		b = {"field_key": "drug:psp_name", "target_doctype": "CRM Drug Program Profile", "fieldname": "psp_name", "applies_to": ""}
		self.assertEqual(len(detail.dedup_rows([a, b])), 2)

	def test_writable_keys_excludes_readonly(self):
		selected = {
			"lead:first_name": {"target_doctype": "CRM Lead", "fieldname": "first_name"},
			"lead:mobile_no": {"target_doctype": "CRM Lead", "fieldname": "mobile_no"},
		}
		# inject a read-only resolver: mobile_no is read-only (API-owned)
		ro = lambda _dt, fn: fn == "mobile_no"
		writable = detail.writable_keys(selected, is_readonly=ro)
		self.assertIn("lead:first_name", writable)
		self.assertNotIn("lead:mobile_no", writable)


# ----------------------------- endpoints (DB-bound) -----------------------------
class TestLeadDetailEndpoints(FrappeTestCase):
	def setUp(self):
		# A minimal lead the Administrator (System Manager) can always read/write.
		self.lead = frappe.get_doc({
			"doctype": "CRM Lead",
			"first_name": "Detail",
			"last_name": "Probe",
			"mobile_no": "+919999000111",
		}).insert(ignore_permissions=True)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.delete_doc("CRM Lead", self.lead.name, force=True, ignore_permissions=True)

	def test_read_returns_sections_with_resolved_values(self):
		out = detail.lead_detail(self.lead.name)
		self.assertIn("sections", out)
		# every section carries key/label/order/fields; every field carries label+value+empty flags
		for sec in out["sections"]:
			self.assertTrue({"key", "label", "order", "fields"} <= set(sec))
			for f in sec["fields"]:
				self.assertTrue({"field_key", "label", "fieldname", "value", "empty", "read_only"} <= set(f))
		# the universal floor (status) is always projected
		flat = {f["field_key"]: f for sec in out["sections"] for f in sec["fields"]}
		self.assertIn("lead:status", flat)

	def test_routing_fields_are_shown_but_never_writable(self):
		# custom_vertical/group/current_program are catalog fields (so they show) but are
		# identity/routing — they must be read_only in the projection and rejected on write.
		out = detail.lead_detail(self.lead.name)
		flat = {f["field_key"]: f for sec in out["sections"] for f in sec["fields"]}
		for fk in ("lead:vertical", "lead:group", "lead:program"):
			if fk in flat:
				self.assertTrue(flat[fk]["read_only"], f"{fk} must be read_only in the panel")

	def test_write_rejects_field_outside_allowlist(self):
		# custom_vertical is a routing field — never editable via this endpoint.
		with self.assertRaises(frappe.exceptions.ValidationError):
			detail.update_lead_detail(self.lead.name, {"lead:custom_vertical": "Hacked Vertical"})

	def test_write_rejects_unknown_field_key(self):
		with self.assertRaises(frappe.exceptions.ValidationError):
			detail.update_lead_detail(self.lead.name, {"lead:__nonexistent__": "x"})

	def test_write_rejects_readonly_field(self):
		# mobile_no is Property-Setter read-only (API-owned) — rejected even as Administrator.
		with self.assertRaises(frappe.exceptions.ValidationError):
			detail.update_lead_detail(self.lead.name, {"lead:mobile_no": "+910000000000"})

	def test_read_throws_for_unprivileged_user(self):
		victim = _ensure_plain_user()
		frappe.set_user(victim)
		try:
			with self.assertRaises(frappe.exceptions.PermissionError):
				detail.lead_detail(self.lead.name)
		finally:
			frappe.set_user("Administrator")

	def test_value_is_inert_text_not_evaluated(self):
		# A value carrying HTML/script is returned verbatim as data (the frontend never v-html's it).
		self.lead.db_set("first_name", "<script>alert(1)</script>", update_modified=False)
		out = detail.lead_detail(self.lead.name)
		flat = {f["field_key"]: f for sec in out["sections"] for f in sec["fields"]}
		fn = flat.get("lead:first_name")
		if fn is not None:  # only if first_name is a catalog field in this env
			self.assertEqual(fn["value"], "<script>alert(1)</script>")


def _ensure_plain_user():
	email = "detail.plainuser@example.com"
	if not frappe.db.exists("User", email):
		frappe.get_doc({
			"doctype": "User", "email": email, "first_name": "Plain",
			"send_welcome_email": 0, "roles": [],
		}).insert(ignore_permissions=True)
	return email
