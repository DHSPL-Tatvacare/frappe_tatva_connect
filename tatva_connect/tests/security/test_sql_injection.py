# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""SQL-injection attack suite for the Smart Views composer (OWASP A05:2025 Injection /
WSTG-INPV-05).

This is the runtime DAST-in-a-test: it fires a corpus of real injection payloads through
EVERY user-controlled input of `tatva_connect.smartview.api.get_data` — free-text search,
ad-hoc filters (value AND operator), sort (key AND direction), the interactive columns
override, and (via the same `_criterion` sink) the saved predicate — and asserts the three
invariants that together mean "not injectable":

  1. NO SQL ERROR        — a payload never raises a database error. The composer either matches
                           nothing or matches safely; it never produces malformed SQL.
  2. NO SCOPE BYPASS     — a tautology ("' OR 1=1 -- ") never widens the row set. A lead the
                           caller is NOT entitled to (the CRM Lead permission_query_conditions —
                           registered by the upstream `crm` fork, ANDed into every composer query
                           by api._pqc_criterion) NEVER appears, and the row count never exceeds
                           the entitled baseline. This is the one that matters: injection must
                           not defeat the grain/PQC scope.
  3. NO IDENTIFIER INJECTION — a column/sort/filter KEY that is not a catalog field_key is
                           dropped, never concatenated into SQL.

Plus a time-based-blind probe: "' OR SLEEP(3) -- " must return fast — proof the value is
escaped (matched as a literal), not executed.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.test_sql_injection
"""
import time

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.smartview import api

# OWASP / SecLists classic SQLi corpus — quote-breakers, comment terminators, UNION, stacked
# statements, the backslash escape-breaker (pypika escapes single quotes, NOT backslashes —
# the one genuine subtlety), and an inline sub-select exfil attempt.
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

FIELD = "sqli:first_name"        # universal catalog field (no grain tag -> visible to every user)
ROLE = "Sales User"
USER = "sqli-attacker@example.com"
VISIBLE_TAG = "ZSQLI_VISIBLE"    # the ONE lead the caller is entitled to
SECRET_TAG = "ZSQLI_SECRET"      # a lead the caller must NEVER see, whatever the payload


class TestSmartViewSqlInjection(FrappeTestCase):
	def setUp(self):
		self._made = []
		self.visible = self.secret = None  # so tearDown never AttributeErrors if setUp throws early
		self._seed_catalog()
		self.user = self._seed_user()
		self.visible = self._seed_lead(VISIBLE_TAG, owner=self.user)
		self.secret = self._seed_lead(SECRET_TAG, owner="Administrator")
		# Restrict the caller to ONLY the visible lead -> the secret lead is out of scope, so the
		# composer's PQC must keep it hidden under every payload.
		self._restrict(self.visible, self.user)
		self.view = self._seed_view()

	def tearDown(self):
		frappe.set_user("Administrator")
		if self.visible:
			frappe.db.delete("User Permission", {"allow": "CRM Lead", "for_value": self.visible})
		for dt, name in reversed(self._made):
			frappe.delete_doc(dt, name, force=True, ignore_permissions=True)

	# --- seeding -------------------------------------------------------------
	def _seed_catalog(self):
		spec = dict(
			field_key=FIELD, label="SQLi First Name", fieldname="first_name", section_key="lead",
			target_doctype="CRM Lead", sql_source="parent", applies_to="lead",
			filterable=1, sortable=1, surface="worklist",
		)
		if frappe.db.exists("CRM Lead API Field", FIELD):
			frappe.get_doc("CRM Lead API Field", FIELD).update(spec).save(ignore_permissions=True)
		else:
			frappe.get_doc(dict(doctype="CRM Lead API Field", **spec)).insert(ignore_permissions=True)
		self._made.append(("CRM Lead API Field", FIELD))

	def _seed_user(self):
		if not frappe.db.exists("User", USER):
			frappe.get_doc({
				"doctype": "User", "email": USER, "first_name": "SQLi Attacker",
				"send_welcome_email": 0, "roles": [{"role": ROLE}],
			}).insert(ignore_permissions=True)
		return USER

	def _seed_lead(self, tag, owner):
		ld = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": tag, "lead_name": tag,
			"status": "New", "lead_owner": owner,
		})
		ld.insert(ignore_permissions=True)
		ld.db_set("owner", owner)
		self._made.append(("CRM Lead", ld.name))
		return ld.name

	def _restrict(self, lead, user):
		frappe.get_doc({
			"doctype": "User Permission", "user": user, "allow": "CRM Lead", "for_value": lead,
		}).insert(ignore_permissions=True)

	def _seed_view(self):
		doc = frappe.get_doc({
			"doctype": "CRM Smart View", "label": "ZSQLI View", "base_object": "Lead",
			"is_standard": 1, "columns": frappe.as_json([FIELD]),
			"predicate": frappe.as_json({"op": "and", "conditions": []}),
		}).insert(ignore_permissions=True)
		self._made.append(("CRM Smart View", doc.name))
		return doc.name

	# --- helpers -------------------------------------------------------------
	def _as_user(self, **kwargs):
		"""Run get_data as the restricted attacker. Returns the result dict; never swallows a
		database error — a raised exception fails the calling assertion with the payload."""
		frappe.set_user(self.user)
		try:
			return api.get_data(self.view, **kwargs)
		finally:
			frappe.set_user("Administrator")

	def _assert_safe(self, label, **kwargs):
		"""A single attack vector: no exception, secret lead never present, count never above
		the entitled baseline."""
		data = None
		try:
			data = self._as_user(**kwargs)
		except Exception as e:  # noqa: BLE001 - any raise here is a finding
			self.fail(f"{label}: get_data RAISED {type(e).__name__}: {e}")
		assert data is not None  # self.fail above raises otherwise; satisfies the type checker
		names = [r.get("name") for r in data["rows"]]
		self.assertNotIn(self.secret, names, f"{label}: SCOPE BYPASS — secret lead leaked")
		self.assertLessEqual(data["total"], 1, f"{label}: row count {data['total']} > entitled baseline (1)")
		return data

	# --- baseline ------------------------------------------------------------
	def test_baseline_scope_is_one_lead(self):
		"""Sanity: with no payload the attacker sees exactly their one entitled lead and not the
		secret — so the bypass assertions below are meaningful."""
		data = self._as_user()
		names = [r.get("name") for r in data["rows"]]
		self.assertEqual(data["total"], 1)
		self.assertIn(self.visible, names)
		self.assertNotIn(self.secret, names)

	# --- the attack ----------------------------------------------------------
	def test_search_payloads(self):
		for p in PAYLOADS:
			self._assert_safe(f"search={p!r}", search=p)

	def test_filter_value_payloads(self):
		for p in PAYLOADS:
			self._assert_safe(f"filter like {p!r}", filters=[[FIELD, "like", p]])
			self._assert_safe(f"filter = {p!r}", filters=[[FIELD, "=", p]])

	def test_filter_operator_injection(self):
		# A bogus operator (incl. an injection string) is skipped, never concatenated.
		for p in PAYLOADS:
			self._assert_safe(f"filter op {p!r}", filters=[[FIELD, p, "x"]])

	def test_sort_key_injection(self):
		# A non-catalog sort KEY is dropped -> falls back to `modified`; never reaches ORDER BY.
		for p in PAYLOADS:
			self._assert_safe(f"sort key {p!r}", sort=[p, "asc"])

	def test_sort_direction_injection(self):
		# Direction is coerced to asc/desc; anything else -> asc. Never raw.
		for p in PAYLOADS:
			self._assert_safe(f"sort dir {p!r}", sort=[FIELD, p])

	def test_columns_identifier_injection(self):
		for p in PAYLOADS:
			data = self._assert_safe(f"columns {p!r}", columns=[p, FIELD])
			keys = [c["key"] for c in data["columns"]]
			self.assertNotIn(p, keys, f"columns {p!r}: injected key surfaced as a real column")

	def test_predicate_value_uses_same_safe_sink(self):
		# The saved predicate runs through the SAME _criterion/_OPS builder as ad-hoc filters
		# (api._predicate_where -> _criterion). Persist a malicious predicate and prove it's safe.
		frappe.db.set_value(
			"CRM Smart View", self.view, "predicate",
			frappe.as_json({"op": "and", "conditions": [
				{"field": FIELD, "operator": "like", "value": "' OR 1=1 -- "},
			]}),
		)
		self._assert_safe("predicate tautology")

	def test_time_based_blind_is_escaped_not_executed(self):
		for vector in ("search", "filter"):
			t0 = time.monotonic()
			if vector == "search":
				self._as_user(search=SLEEP_PAYLOAD)
			else:
				self._as_user(filters=[[FIELD, "like", SLEEP_PAYLOAD]])
			elapsed = time.monotonic() - t0
			self.assertLess(
				elapsed, SLEEP_CEILING,
				f"time-based blind via {vector}: {elapsed:.2f}s — SLEEP() may have EXECUTED",
			)
