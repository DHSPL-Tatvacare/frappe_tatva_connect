# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A composite key is the database's identity; the LABEL is the user's. Both directions, proven.

`labels.py` mapped key -> label in five functions and label -> key in none, so every surface that ASKS
rather than SHOWS had to compare raw keys. Measured on 2026-08-04: `CRM Lead Stage` holds 292 rows under
107 names, so a sub-stage filter offered `Not Interested` four times and each one matched a single
programme's leads.

These tests hold the two new directions against the one that already existed. The round-trip is the
lock: every key `keys_of` returns must title back through `title_of` to the label asked for, so the two
cannot drift apart whatever a master renames.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.taxonomy.test_composite_key_labels
"""
import unittest

import frappe

from tatva_connect.list_engine import engine
from tatva_connect.smartview import api as smartview
from tatva_connect.taxonomy import labels

# The masters a rep actually picks from. Named per the inventory, not guessed: each is a composite key
# that something offers in a Link picker.
OFFERED = ("CRM Lead Stage", "CRM Picklist Value", "CRM Task Type")


class TestTitleField(unittest.TestCase):
	"""One reader of `title_field`, so the three directions cannot disagree about where a label lives."""

	def test_every_offered_master_declares_one(self):
		for doctype in OFFERED:
			self.assertTrue(labels.title_field(doctype), f"{doctype} offers no label to read")

	def test_a_doctype_naming_its_own_name_has_no_label(self):
		self.assertIsNone(labels.title_field("CRM Lead Source"))

	def test_an_unknown_doctype_degrades_rather_than_raising(self):
		self.assertIsNone(labels.title_field("No Such Doctype"))


class TestLabelsOf(unittest.TestCase):
	"""The distinct labels a user picks from — fewer than the rows wherever a grain repeats a name."""

	def setUp(self):
		self.sp = f"labels_{frappe.generate_hash(length=6)}"
		frappe.db.savepoint(self.sp)

	def tearDown(self):
		frappe.db.rollback(save_point=self.sp)

	def test_a_repeated_name_is_offered_once(self):
		rows = frappe.db.count("CRM Lead Stage")
		offered = labels.labels_of("CRM Lead Stage")
		self.assertEqual(len(offered), len(set(offered)), "the same label was offered twice")
		self.assertLess(len(offered), rows, "the master repeats a name, so fewer labels than rows")

	def test_it_reads_the_master_rather_than_a_declaration(self):
		"""A row added now is offered now. This is what a stored bucket list cannot do."""
		before = labels.labels_of("CRM Lead Stage")
		probe = "ZZ Probe Stage"
		self.assertNotIn(probe, before)
		frappe.get_doc({"doctype": "CRM Lead Stage", "program": "Sigrima", "stage": probe,
		                "display_label": probe}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, rolled back
		self.assertIn(probe, labels.labels_of("CRM Lead Stage"))

	def test_txt_narrows_without_changing_the_rule(self):
		narrowed = labels.labels_of("CRM Lead Stage", "Not Inter")
		self.assertTrue(narrowed)
		self.assertTrue(all("not inter" in v.lower() for v in narrowed))

	def test_a_doctype_with_no_label_offers_nothing_and_is_left_alone(self):
		self.assertEqual(labels.labels_of("CRM Lead Source"), [])


class TestKeysOf(unittest.TestCase):
	"""The inverse. A label means every key carrying it; a key means itself."""

	def test_one_label_returns_every_grain_that_carries_it(self):
		keys = labels.keys_of("CRM Lead Stage", "Not Interested")
		self.assertGreater(len(keys), 1, "the fixture no longer repeats this name across programmes")
		self.assertEqual(len(keys), len(set(keys)))

	def test_the_round_trip_holds_for_every_offered_master(self):
		"""THE LOCK. Every key returned must title back to the label asked for, on every master."""
		for doctype in OFFERED:
			for label in labels.labels_of(doctype)[:25]:
				keys = labels.keys_of(doctype, label)
				self.assertTrue(keys, f"{doctype}: {label!r} resolved to no key")
				self.assertEqual(
					{labels.title_of(doctype, k) for k in keys}, {label},
					f"{doctype}: a key returned for {label!r} does not title back to it",
				)

	def test_a_value_that_is_already_a_key_stays_exact(self):
		key = frappe.db.get_value("CRM Lead Stage", {}, "name", order_by="name asc")
		self.assertEqual(labels.keys_of("CRM Lead Stage", key), [key])

	def test_an_ordinary_doctype_falls_through_untouched(self):
		self.assertEqual(labels.keys_of("CRM Lead Source", "Mobile App"), ["Mobile App"])

	def test_an_unknown_label_returns_itself_rather_than_everything(self):
		"""Never widen. An unmatched value must not resolve to the whole master."""
		self.assertEqual(labels.keys_of("CRM Lead Stage", "ZZ No Such Stage"), ["ZZ No Such Stage"])


class TestLabelQuery(unittest.TestCase):
	"""What a FILTER picker is offered. `search_link`'s custom-query seam, same shape as picklist_query."""

	def setUp(self):
		self.sp = f"labelq_{frappe.generate_hash(length=6)}"
		frappe.db.savepoint(self.sp)

	def tearDown(self):
		frappe.db.rollback(save_point=self.sp)

	def offered(self, doctype, txt="", page_len=20):
		return labels.label_query(doctype, txt, "name", 0, page_len, None)

	def test_a_repeated_name_is_offered_once(self):
		rows = self.offered(labels.LEAD_STAGE, "Not Interested")
		self.assertEqual(rows, [("Not Interested", "Not Interested")])
		self.assertGreater(len(labels.keys_of(labels.LEAD_STAGE, "Not Interested")), 1)

	def test_value_and_label_are_both_the_label(self):
		"""A filter matches on what the reader picked, so the option cannot carry a key it did not show."""
		for value, label in self.offered(labels.LEAD_STAGE, "", 200):
			self.assertEqual(value, label)

	def test_an_ordinary_doctype_is_answered_by_nobody_here(self):
		self.assertEqual(self.offered("CRM Lead Source"), [])

	def test_the_cap_is_the_query_s_own_and_not_the_caller_s(self):
		self.assertEqual(len(self.offered(labels.LEAD_STAGE, "", 5000)), labels.OPTION_CAP)

	def test_it_reads_the_master_rather_than_a_declaration(self):
		probe = "ZZ Probe Offered Stage"
		self.assertNotIn((probe, probe), self.offered(labels.LEAD_STAGE, probe))
		frappe.get_doc({"doctype": labels.LEAD_STAGE, "program": "Sigrima", "stage": probe,
		                "display_label": probe}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, rolled back
		self.assertIn((probe, probe), self.offered(labels.LEAD_STAGE, probe))


class TestLinkQuery(unittest.TestCase):
	"""WHICH masters get the scoped query is one server-side decision, relayed to every menu."""

	def test_a_composite_master_names_the_label_query(self):
		for doctype in OFFERED:
			self.assertIn(doctype, labels.COMPOSITE)
			self.assertEqual(labels.link_query(doctype), labels.LABEL_QUERY)

	def test_an_ordinary_doctype_keeps_the_framework_s_own_search(self):
		self.assertIsNone(labels.link_query("CRM Lead Source"))
		self.assertIsNone(labels.link_query(None))

	def test_the_ingestion_registry_names_no_master_this_one_does_not_know(self):
		"""`picklist._COMPOSITE_PK_MASTERS` answers a DIFFERENT question — how to resolve a human value to
		a PK on the way IN, which needs a per-master resolver `labels` has no business holding. But both
		assert the same underlying fact, that the master's key is a composite, so the ingestion registry
		may only name masters this one already knows. Without this they drift into disagreeing about what
		a composite is, which is how a value resolves on write and stays unfilterable on read."""
		from tatva_connect.taxonomy import picklist

		unknown = sorted(set(picklist._COMPOSITE_PK_MASTERS) - set(labels.COMPOSITE))
		self.assertEqual(unknown, [], f"resolvers registered for masters labels.COMPOSITE omits: {unknown}")


class TestFilterOn(unittest.TestCase):
	"""The ONE matching rule both engines call. A label is a set of keys; everything else is untouched."""

	def test_an_equality_on_a_label_becomes_a_membership_test(self):
		operator, value = labels.filter_on(labels.LEAD_STAGE, "=", "Not Interested")
		self.assertEqual(operator, "in")
		self.assertEqual(sorted(value), sorted(labels.keys_of(labels.LEAD_STAGE, "Not Interested")))
		self.assertGreater(len(value), 1)

	def test_a_negation_becomes_a_negated_membership_test(self):
		operator, value = labels.filter_on(labels.LEAD_STAGE, "!=", "Not Interested")
		self.assertEqual(operator, "not in")
		self.assertGreater(len(value), 1)

	def test_a_value_that_is_already_a_key_is_returned_exactly_as_it_arrived(self):
		"""THE SAVED-VIEW LOCK. A stored raw key must keep asking the question it always asked."""
		key = frappe.db.get_value(labels.LEAD_STAGE, {}, "name", order_by="name asc")
		self.assertEqual(labels.filter_on(labels.LEAD_STAGE, "=", key), ("=", key))

	def test_an_in_list_expands_every_member_and_keeps_them_distinct(self):
		operator, value = labels.filter_on(labels.LEAD_STAGE, "in", ["Not Interested", "Not Interested"])
		self.assertEqual(operator, "in")
		self.assertEqual(len(value), len(set(value)))

	def test_an_operator_that_is_not_a_membership_test_is_left_alone(self):
		self.assertEqual(labels.filter_on(labels.LEAD_STAGE, "like", "Not"), ("like", "Not"))

	def test_an_ordinary_doctype_is_left_alone(self):
		self.assertEqual(labels.filter_on("CRM Lead Source", "=", "Mobile App"), ("=", "Mobile App"))

	def test_an_unknown_value_never_widens(self):
		self.assertEqual(labels.filter_on(labels.LEAD_STAGE, "=", "ZZ No Such"), ("=", "ZZ No Such"))


class TestTheListEngineReadsLabels(unittest.TestCase):
	"""The listing seam. It sits above the derived-field fork, so every doctype's list obeys it."""

	def read(self, **kwargs):
		return engine._read_labels({"doctype": "CRM Lead", **kwargs})

	def test_a_label_filter_is_read_as_every_key_it_means(self):
		out = self.read(filters={"custom_substage": "Not Interested"})
		operator, keys = out["filters"]["custom_substage"]
		self.assertEqual(operator, "in")
		self.assertEqual(sorted(keys), sorted(labels.keys_of(labels.LEAD_STAGE, "Not Interested")))

	def test_a_request_naming_no_label_comes_back_as_the_very_object_it_arrived_as(self):
		key = frappe.db.get_value(labels.LEAD_STAGE, {}, "name", order_by="name asc")
		kwargs = {"doctype": "CRM Lead", "filters": {"custom_substage": key, "status": "Open"}}
		self.assertIs(engine._read_labels(kwargs), kwargs)

	def test_a_field_that_is_not_a_link_never_reaches_the_rule(self):
		kwargs = {"doctype": "CRM Lead", "filters": {"lead_name": "Not Interested"}}
		self.assertIs(engine._read_labels(kwargs), kwargs)

	def test_a_saved_view_s_default_filters_are_prepared_too(self):
		out = self.read(default_filters={"custom_substage": "Not Interested"})
		self.assertEqual(out["default_filters"]["custom_substage"][0], "in")

	def test_a_json_payload_comes_back_as_json(self):
		out = self.read(filters=frappe.as_json({"custom_substage": "Not Interested"}))
		self.assertIsInstance(out["filters"], str)
		self.assertEqual(frappe.parse_json(out["filters"])["custom_substage"][0], "in")


class TestTheSmartViewReadsLabels(unittest.TestCase):
	"""The other engine, asking the SAME function — a filter cannot mean two things on two surfaces."""

	def column(self, fieldtype, options):
		return frappe._dict(fieldtype=fieldtype, options=options, filterable=1, fieldname="custom_substage")

	def criterion(self, value):
		cat = {"k": self.column("Link", labels.LEAD_STAGE)}
		terms = {"k": frappe.qb.DocType("CRM Lead").custom_substage}
		return str(smartview._apply_filters(None, [["k", "=", value]], cat, terms))

	def test_the_link_target_is_read_off_the_catalog_column(self):
		self.assertEqual(smartview._link_master(self.column("Link", labels.LEAD_STAGE)), labels.LEAD_STAGE)
		self.assertIsNone(smartview._link_master(self.column("Data", "")))

	def test_a_label_becomes_an_in_over_every_key(self):
		sql = self.criterion("Not Interested")
		self.assertIn("IN (", sql)
		for key in labels.keys_of(labels.LEAD_STAGE, "Not Interested"):
			self.assertIn(key, sql)

	def test_a_raw_key_stays_an_equality(self):
		key = frappe.db.get_value(labels.LEAD_STAGE, {}, "name", order_by="name asc")
		sql = self.criterion(key)
		self.assertNotIn("IN (", sql)
		self.assertIn(key, sql)


class TestTheInvariantThatKeepsThisTrue(unittest.TestCase):
	"""A composite master offered in a picker MUST declare a label, or it silently keeps the old
	behaviour and a rep reads a raw key. This is the guard that fails when a sixth master is added."""

	def test_every_composite_master_offered_in_a_picker_declares_a_label(self):
		composite = set(frappe.get_all("DocType", filters={"autoname": ["like", "%::%"]}, pluck="name"))
		targets = set()
		for doctype in ("Custom Field", "DocField"):
			targets |= {
				r.options
				for r in frappe.get_all(doctype, filters={"fieldtype": ["in", ("Link", "Table MultiSelect")]},
				                        fields=["options"], distinct=True)
				if r.options
			}
		missing = sorted(dt for dt in composite & targets if not labels.title_field(dt))
		self.assertEqual(
			missing, [],
			f"composite masters offered in a picker with no title_field: {missing}. "
			"Declare one, or the picker shows a rep the raw composite key.",
		)

	def test_every_composite_master_offered_in_a_picker_is_wired_or_exempt(self):
		"""The second half of the guard: declaring a label is not the same as USING it.

		`labels.COMPOSITE` is what the filter surfaces treat as label-identified, and a master that grows a
		picker without joining it keeps the old behaviour silently — four `Not Interested` options, each
		matching one programme. Adding a master to COMPOSITE or to EXEMPT is a decision someone makes on
		purpose; growing one and doing neither is not."""
		exempt = {
			# Operator config, never on a rep's filter bar: the key IS what an admin is choosing between.
			"CRM Lead API Mapping",
			"CRM Hospital",
		}
		composite = set(frappe.get_all("DocType", filters={"autoname": ["like", "%::%"]}, pluck="name"))
		targets = set()
		for doctype in ("Custom Field", "DocField"):
			targets |= {
				r.options
				for r in frappe.get_all(doctype, filters={"fieldtype": ["in", ("Link", "Table MultiSelect")]},
				                        fields=["options"], distinct=True)
				if r.options
			}
		unwired = sorted((composite & targets) - set(labels.COMPOSITE) - exempt)
		self.assertEqual(
			unwired, [],
			f"composite masters offered in a picker and wired to nothing: {unwired}. "
			"Add each to labels.COMPOSITE, or to this test's exempt set with the reason.",
		)
