# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Injection payloads through every user input of the Smart Views `get_data` never raise a SQL error,
widen the row scope, inject an identifier, or execute a SLEEP."""
import time

import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.smartview import api
from tatva_connect.tests.authz.grains import GRAINS

# Classic SQLi corpus; the backslash case matters because pypika escapes quotes but not backslashes.
PAYLOADS = [
	"' OR '1'='1",
	"' OR 1=1 -- ",
	"') OR ('1'='1",
	"'; DROP TABLE `tabCRM Lead`; -- ",
	"' UNION SELECT name, 1 FROM `tabUser` -- ",
	"\\' OR 1=1 -- ",
	"1) OR (1=1",
	"admin'--",
	"' OR ''='",
	"%27%20OR%201=1%20--%20",
	"'||(SELECT password FROM `tabUser` LIMIT 1)||'",
	'" OR "1"="1',
]
# Time-based blind: SLEEP(3) per matched row if executed; ~0s if escaped to a literal.
SLEEP_PAYLOAD = "' OR SLEEP(3) -- "
SLEEP_CEILING = 2.0

ROLE = "Sales User"
USER = "sqli-attacker@example.com"
VISIBLE_TAG = "ZSQLI_VISIBLE"    # the ONE lead the caller is entitled to
SECRET_TAG = "ZSQLI_SECRET"      # a lead the caller must NEVER see, whatever the payload


class TestSmartViewSqlInjection(IntegrationTestCase):
	"""One attacker who may see exactly one lead; every payload must leave it at one and never reach the secret lead."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.field = cls._catalogued("first_name")
		if not frappe.db.exists("User", USER):
			frappe.get_doc({"doctype": "User", "email": USER, "first_name": "SQLi Attacker", "send_welcome_email": 0,
			                "roles": [{"role": ROLE}]}).insert(ignore_permissions=True)
		cls.user = USER
		cls._entitle(GRAINS[0], cls.field)
		cls.visible = cls._lead(VISIBLE_TAG, USER)
		cls.secret = cls._lead(SECRET_TAG, "Administrator")
		frappe.get_doc({"doctype": "User Permission", "user": USER, "allow": "CRM Lead", "for_value": cls.visible}).insert(
			ignore_permissions=True)
		cls.view = frappe.get_doc({
			"doctype": "CRM Smart View", "label": "ZSQLI View", "base_object": "Lead", "is_standard": 1,
			"columns": frappe.as_json([cls.field]), "predicate": frappe.as_json({"op": "and", "conditions": []}),
		}).insert(ignore_permissions=True).name

	@staticmethod
	def _catalogued(fieldname):
		"""The one catalog row for this lead column, filterable and sortable; a field has exactly one row."""
		key = frappe.db.get_value("CRM Lead API Field", {"section": "lead", "fieldname": fieldname})
		if not key:
			key = frappe.get_doc({"doctype": "CRM Lead API Field", "field_key": f"lead:{fieldname}", "label": fieldname,
			                      "fieldname": fieldname, "section": "lead"}).insert(ignore_permissions=True).name
		frappe.get_doc("CRM Lead API Field", key).update({"filterable": 1, "sortable": 1}).save(ignore_permissions=True)
		return key

	@staticmethod
	def _entitle(g, field):
		"""Entitle the attacker to the field the way production does: an Assignment Rule at a grain whose contract ticks it."""
		frappe.get_doc({"doctype": "Assignment Rule", "name": "sqli-attacker-rule", "document_type": "CRM Lead",
		                "assign_condition": "1", "rule": "Round Robin", "priority": 0, "disabled": 0,
		                "grain_vertical": g["vertical"], "grain_group": g["group"], "grain_program": g["program"],
		                "users": [{"user": USER}], "assignment_days": [{"day": "Monday"}]}).insert(ignore_permissions=True)
		contract = frappe.db.get_value("CRM Lead API Mapping", {"is_internal": 1, "vertical": g["vertical"],
		                                "crm_group": g["group"], "program": g["program"]}) or frappe.get_doc({
			"doctype": "CRM Lead API Mapping", "contract_name": "sqli internal", "enabled": 1, "is_internal": 1,
			"vertical": g["vertical"], "crm_group": g["group"], "program": g["program"]}).insert(ignore_permissions=True).name
		doc = frappe.get_doc("CRM Lead API Mapping", contract)
		if field not in [r.field for r in doc.allowed_fields]:
			doc.append("allowed_fields", {"field": field})
			doc.save(ignore_permissions=True)
		for bucket in ("tatva_connect:entitled_grains", "tatva_connect:internal_contract_ticks", "tatva_connect:internal_universal_fields"):
			if hasattr(frappe.local, bucket):
				delattr(frappe.local, bucket)

	@staticmethod
	def _lead(tag, owner):
		g = GRAINS[0]
		return frappe.get_doc({"doctype": "CRM Lead", "first_name": tag, "lead_name": tag, "status": "New", "lead_owner": owner,
		                       "custom_vertical": g["vertical"], "custom_group": g["group"],
		                       "custom_current_program": g["program"]}).insert(ignore_permissions=True).name

	# --- helpers -------------------------------------------------------------
	def _as_user(self, **kwargs):
		"""Run get_data as the attacker; a database error propagates to fail the caller with the payload."""
		frappe.set_user(self.user)
		try:
			return api.get_data(self.view, **kwargs)
		finally:
			frappe.set_user("Administrator")

	def _assert_safe(self, label, **kwargs):
		"""One attack vector: no exception, no secret lead, and no more rows than the entitled baseline."""
		try:
			data = self._as_user(**kwargs)
		except frappe.ValidationError:
			return None  # refused before any SQL ran: a safe answer
		except Exception as e:
			self.fail(f"{label}: get_data RAISED {type(e).__name__}: {e}")
		names = [r.get("name") for r in data["rows"]]
		self.assertNotIn(self.secret, names, f"{label}: SCOPE BYPASS — secret lead leaked")
		self.assertLessEqual(data["total"], 1, f"{label}: row count {data['total']} > entitled baseline (1)")
		return data

	# --- baseline ------------------------------------------------------------
	def test_baseline_scope_is_one_lead(self):
		"""With no payload the attacker sees exactly their one lead, so the bypass checks mean something."""
		data = self._as_user()
		names = [r.get("name") for r in data["rows"]]
		self.assertEqual(data["total"], 1)
		self.assertIn(self.visible, names)
		self.assertNotIn(self.secret, names)
		# The filter and sort sinks must be live for the attacker, or every payload test below passes by refusal alone.
		live = self._as_user(filters=[[self.field, "like", VISIBLE_TAG]], sort=[self.field, "desc"])
		self.assertEqual([r.get("name") for r in live["rows"]], [self.visible])

	# --- the attack ----------------------------------------------------------
	def test_search_payloads(self):
		for p in PAYLOADS:
			self._assert_safe(f"search={p!r}", search=p)

	def test_filter_value_payloads(self):
		for p in PAYLOADS:
			self._assert_safe(f"filter like {p!r}", filters=[[self.field, "like", p]])
			self._assert_safe(f"filter = {p!r}", filters=[[self.field, "=", p]])

	def test_filter_operator_injection(self):
		# A bogus operator (incl. an injection string) is skipped, never concatenated.
		for p in PAYLOADS:
			self._assert_safe(f"filter op {p!r}", filters=[[self.field, p, "x"]])

	def test_sort_key_injection(self):
		# A non-catalog sort key is dropped and never reaches ORDER BY.
		for p in PAYLOADS:
			self._assert_safe(f"sort key {p!r}", sort=[p, "asc"])

	def test_sort_direction_injection(self):
		# Anything other than asc or desc becomes asc.
		for p in PAYLOADS:
			self._assert_safe(f"sort dir {p!r}", sort=[self.field, p])

	def test_columns_identifier_injection(self):
		for p in PAYLOADS:
			data = self._assert_safe(f"columns {p!r}", columns=[p, self.field])
			keys = [c["key"] for c in data["columns"]] if data else []
			self.assertNotIn(p, keys, f"columns {p!r}: injected key surfaced as a real column")

	def test_predicate_value_uses_same_safe_sink(self):
		# A saved predicate goes through the same _criterion builder as ad-hoc filters.
		view = frappe.get_doc("CRM Smart View", self.view)
		view.predicate = frappe.as_json({"op": "and", "conditions": [{"field": self.field, "operator": "like", "value": "' OR 1=1 -- "}]})
		view.save(ignore_permissions=True)
		self._assert_safe("predicate tautology")

	def test_time_based_blind_is_escaped_not_executed(self):
		for vector in ("search", "filter"):
			t0 = time.monotonic()
			if vector == "search":
				self._as_user(search=SLEEP_PAYLOAD)
			else:
				self._as_user(filters=[[self.field, "like", SLEEP_PAYLOAD]])
			elapsed = time.monotonic() - t0
			self.assertLess(
				elapsed, SLEEP_CEILING,
				f"time-based blind via {vector}: {elapsed:.2f}s — SLEEP() may have EXECUTED",
			)
