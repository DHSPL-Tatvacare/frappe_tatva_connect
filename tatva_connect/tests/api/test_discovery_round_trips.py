# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""What discovery advertises, the write accepts and the read returns.

THE CLASS. A value crossing the partner boundary is spoken in two languages: the caller's, which is a
human label, and the database's, which for a grain-scoped master is a composite `::` key. Three defects
were one hole — the activity family never passed its values through the translation seam the lead family
uses, so `plan` was refused on write, echoed its raw key on read, and resolved to whichever condition's
row came first because a label at a cascading master is not unique.

Patching those three would have left the fourth. This closes the loop instead: for every value discovery
advertises, sending it must be accepted, and reading it back must return the same word that was
advertised. A family that skips a seam cannot pass.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.api.test_discovery_round_trips
"""
import unittest

import frappe

from tatva_connect.activity import api as activity_brain
from tatva_connect.api import partner_activity
from tatva_connect.taxonomy import grain, labels, picklist

GRAIN = {"custom_vertical": "Goodflip", "custom_group": "India"}


class DiscoveryCase(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		cls.lead = cls.task_type = None
		for name in frappe.get_all("CRM Lead", filters=GRAIN, pluck="name", limit=200):
			for t in activity_brain.list_types_for_lead(name):
				cfg = activity_brain._type_config(t["name"])
				if any(f.fieldtype == "Link" and (f.options or "") == labels.PICKLIST_VALUE
				       for f in (cfg or {}).get("fields") or []):
					cls.lead, cls.task_type = name, t["name"]
					break
			if cls.lead:
				break

	def setUp(self):
		if not self.lead:
			self.skipTest("no Goodflip lead on this bench runs a type with a picklist Link")
		self._form = frappe.form_dict

	def tearDown(self):
		frappe.form_dict = self._form
		frappe.db.rollback()

	def described(self):
		frappe.local.response = frappe._dict()
		frappe.form_dict = frappe._dict({"lead": self.lead, "task_type": self.task_type})
		partner_activity.activity_schema()
		return dict(frappe.local.response)["data"]["task_types"][0]["fields"]


class TestAdvertisedValuesAreWritable(DiscoveryCase):
	def test_every_advertised_picklist_value_is_writable(self):
		"""Through the WRITE seam, not an internal: `_to_keys` is the whole path a payload takes."""
		axes = grain.of("CRM Lead", self.lead)
		cfg = activity_brain._type_config(self.task_type)
		checked = 0
		for f in self.described():
			if f["type"] != "Link" or not f.get("allowed_values"):
				continue
			for word in f["allowed_values"]:
				key = partner_activity._to_keys(cfg, {f["fieldname"]: word}, axes)[f["fieldname"]]
				checked += 1
				self.assertTrue(
					frappe.db.exists(labels.PICKLIST_VALUE, key),
					f"`{f['fieldname']}` advertises `{word}` and the write resolves it to nothing",
				)
		if not checked:
			self.skipTest("this type advertises no picklist vocabulary")

	def test_a_cascading_value_resolves_to_the_row_its_parent_names(self):
		"""A label at a cascading master is not unique: the key encodes the parent's answer too, so the
		sibling that narrows it has to reach the resolver or an arbitrary row wins."""
		axes = grain.of("CRM Lead", self.lead)
		for f in self.described():
			parent = f.get("controlled_by")
			if not parent or not f.get("allowed_values"):
				continue
			for value in f["allowed_values"]:
				rows = frappe.get_all(
					labels.PICKLIST_VALUE,
					filters={"category": picklist.category_of(f["fieldname"]), "value": value},
					fields=["name", "depends_on_value"],
				)
				for row in rows:
					if not row.depends_on_value:
						continue
					pk = picklist.resolve_value(labels.PICKLIST_VALUE, value, axes, f["fieldname"],
					                            answers={parent: row.depends_on_value})
					self.assertEqual(
						frappe.db.get_value(labels.PICKLIST_VALUE, pk, "depends_on_value"),
						row.depends_on_value,
						f"`{value}` under `{parent}={row.depends_on_value}` resolved to another row",
					)
				return
		self.skipTest("this type has no cascading picklist field")


class TestAdvertisedValuesAreReadable(DiscoveryCase):
	def test_a_stored_key_reads_back_as_the_word_that_was_advertised(self):
		"""RED before this: `plan` echoed `plan::Goodflip::India::Inside-Sales::…::PCOS`, which is not a
		value discovery ever named and not one the caller could send again."""
		axes = grain.of("CRM Lead", self.lead)
		cfg = activity_brain._type_config(self.task_type)
		for f in self.described():
			if f["type"] != "Link" or not f.get("allowed_values"):
				continue
			for word in f["allowed_values"]:
				stored = partner_activity._to_keys(cfg, {f["fieldname"]: word}, axes)
				read = partner_activity._to_labels(cfg, dict(stored))
				self.assertEqual(read[f["fieldname"]], word,
				                 f"`{f['fieldname']}` does not read back the word it advertised")
			return
		self.skipTest("this type advertises no picklist vocabulary")


class TestEverySelectAdvertisesItsVocabulary(DiscoveryCase):
	def test_no_select_is_published_without_the_values_it_takes(self):
		"""A Select the write holds to a list, published with none, is a field a caller must guess at —
		which is what a lead-sourced one did, its vocabulary being declared on the lead."""
		silent = [f["fieldname"] for f in self.described()
		          if f["type"] == "Select" and not f.get("allowed_values")
		          and (f.get("options") or _lead_declares(f["fieldname"]))]
		self.assertEqual(silent, [], "these Selects take a fixed vocabulary and advertise none")


def _lead_declares(fieldname):
	df = frappe.get_meta("CRM Lead").get_field(fieldname)
	return bool(df and df.fieldtype == "Select" and (df.options or "").strip())
