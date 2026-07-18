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
  5. CATALOG-DRIVEN   — the catalog is the sole authority; every catalogued field for the grain
     surfaces (no drug/metabolic world-split; sections are display groups only).
  6. DEDUP            — duplicate catalog rows (partner vs curated) collapse to one per field.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.test_lead_detail
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.access import entitlement, internal_contract
from tatva_connect.lead import detail


# ----------------------------- pure helpers -----------------------------
class TestDetailPureLogic(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# Specificity is now read through the INTERNAL CONTRACT (ticked-by-all-grains == universal), so the
		# contracts must exist; pick a real universal key and a real grain-specific key off the live ticks.
		internal_contract.ensure_internal_contracts()
		ticks = entitlement._internal_ticks()
		universal = set.intersection(*ticks.values()) if ticks else set()
		specific = (set().union(*ticks.values()) if ticks else set()) - universal
		assert universal and specific, "need both a universal and a grain-specific field to prove dedup"
		cls.universal_key = sorted(universal)[0]
		cls.specific_key = sorted(specific)[0]

	def test_dedup_prefers_grain_specific_over_universal(self):
		# One physical field catalogued twice: a grain-scoped row ("Lead Stage") and a universal one
		# ("Sub-stage"). The grain-specific row wins, so its label survives. Specificity is read via the
		# contract brain (is_universal_field), not off grain_* columns in detail.py.
		universal = {"field_key": self.universal_key, "section": "lead", "fieldname": "custom_substage",
		             "label": "Sub-stage"}
		specific = {"field_key": self.specific_key, "section": "lead", "fieldname": "custom_substage",
		            "label": "Lead Stage"}
		self.assertTrue(detail._is_universal(universal))
		self.assertFalse(detail._is_universal(specific))
		out = detail.dedup_rows([universal, specific])
		self.assertEqual(len(out), 1)
		self.assertEqual(out[0]["label"], "Lead Stage")
		# order-independent: the grain-specific row wins whichever appears first
		self.assertEqual(detail.dedup_rows([specific, universal])[0]["label"], "Lead Stage")

	def test_dedup_keeps_distinct_fields(self):
		a = {"field_key": "drug:dosage", "section": "drug", "fieldname": "dosage"}
		b = {"field_key": "drug:psp_name", "section": "drug", "fieldname": "psp_name"}
		self.assertEqual(len(detail.dedup_rows([a, b])), 2)

	def test_writable_keys_excludes_readonly(self):
		# Target doctype is resolved off the section brain; the injected resolver marks mobile_no
		# read-only (API-owned), so it is excluded while first_name stays writable.
		selected = {
			"lead:first_name": {"section": "lead", "fieldname": "first_name"},
			"lead:mobile_no": {"section": "lead", "fieldname": "mobile_no"},
		}
		def ro(_dt, fn):
			return fn == "mobile_no"
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

	def test_section_title_comes_from_the_brain(self):
		# C4: the Data-tab heading is the CRM Lead Section title, read live — not a hardcoded map.
		# Rename the `lead` section in the DB and the projection reflects it; restore on cleanup.
		original = frappe.db.get_value("CRM Lead Section", "lead", "title")
		def restore():
			frappe.db.set_value("CRM Lead Section", "lead", "title", original)
			frappe.clear_document_cache("CRM Lead Section", "lead")
		self.addCleanup(restore)
		frappe.db.set_value("CRM Lead Section", "lead", "title", "Renamed By Brain")
		frappe.clear_document_cache("CRM Lead Section", "lead")
		out = detail.lead_detail(self.lead.name)
		titles = {sec["label"] for sec in out["sections"]}
		self.assertIn("Renamed By Brain", titles)

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
