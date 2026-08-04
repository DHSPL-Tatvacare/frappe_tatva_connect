# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""What a partner is allowed to see, and what they must never be told.

`test_composite_pk_labels` already locks the `::` half — a primary key must not reach a human. This
locks the rest of the same idea for the partner surface, where the reader is a machine on someone
else's estate and every string we hand over is a contract we cannot take back:

  the vocabulary   a response names a home in the words the request accepts (`activity`, `note`),
                   never the table behind it. A partner cannot send `FCRM Note` anywhere, so telling
                   them their file lives on one is a fact they can do nothing with — and it freezes a
                   fork-legacy table name into a public contract forever.
  the file link    a signed link carries its own authorisation, so it is the ONE thing we publish that
                   works without our token. It must therefore be scoped to one blob, carry an expiry a
                   caller can read, and expire.
  one brain        the read side and the write side of a resource resolve through the same declaration,
                   so a shape a response advertises is a shape a request can use.

Every assertion here is about a payload a partner receives, so each one drives the real projection
rather than re-deriving what it should have said.
"""
import unittest

import frappe
from frappe.tests import IntegrationTestCase

VERTICAL, GROUP = "Goodflip-Care", "Anaya"

# The tables behind the partner surface. A response may name none of them.
INTERNAL_TABLES = ("CRM Lead", "CRM Task", "FCRM Note", "CRM Call Log", "CRM Lead Stage",
                   "CRM Task Type", "CRM Picklist Value", "CRM Lead Section", "CRM Lead API Field")


def table_names_in(payload):
	"""Every internal table name reaching the caller, with the path that carries it."""
	found = []

	def walk(node, path="$"):
		if isinstance(node, str):
			for table in INTERNAL_TABLES:
				if table in node:
					found.append(f"{path} names {table!r}: {node[:60]!r}")
		elif isinstance(node, dict):
			for k, v in node.items():
				walk(v, f"{path}.{k}")
		elif isinstance(node, (list, tuple)):
			for i, v in enumerate(node):
				walk(v, f"{path}[{i}]")

	walk(payload)
	return found


class TestTheFileHomeVocabulary(IntegrationTestCase):
	"""A file's home is named in the words the attach accepts, from ONE declaration."""

	def test_every_home_a_request_accepts_is_a_word_a_response_returns(self):
		from tatva_connect.api import partner_file as pf

		for key, _doctype in pf._TARGETS:
			self.assertIn(key, pf._HOME_KEYS.values(),
			              f"`{key}` is accepted by file_attach but no response can say it")

	def test_every_table_a_file_can_hang_from_has_a_word(self):
		from tatva_connect.api import partner_file as pf

		for doctype in ("CRM Lead", *pf._TARGET_DOCTYPES):
			self.assertIn(doctype, pf._HOME_KEYS,
			              f"a file homed on {doctype} would be reported by its table name")
			self.assertNotIn("CRM", pf._HOME_KEYS[doctype], "the word must not be the table name")

	def test_the_two_vocabularies_are_one_set(self):
		"""`_HOME_KEYS` is DERIVED from `_TARGETS`, so adding a home cannot leave the read side behind."""
		from tatva_connect.api import partner_file as pf

		self.assertEqual(
			set(pf._HOME_KEYS.values()) - {"lead"},
			{key for key, _dt in pf._TARGETS},
			"the read vocabulary and the write vocabulary have drifted apart",
		)


class TestTheFileLink(IntegrationTestCase):
	"""The one URL we publish that works without our token."""

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.set_user("Administrator")

	def test_a_local_file_keeps_the_proxy_route_and_never_claims_an_expiry(self):
		"""Nothing to sign means nothing expires — an `expires_at` here would be a promise we cannot keep."""
		from tatva_connect.storage import file_manager

		doc = frappe._dict({"file_url": "/private/files/not-offloaded.txt"})
		url = file_manager.fetch_url(doc)
		self.assertTrue(url.startswith("http"), "an off-site caller cannot use a relative url")
		self.assertIn("/private/files/", url, "a local file must keep its own route")
		self.assertIsNone(file_manager.fetch_expires_at(doc))

	def test_the_expiry_is_read_off_the_token_not_computed(self):
		"""`sas_url` caches, so a second caller inherits the FIRST token's remaining life. Computing
		now+ttl would over-promise by however much of the window had already gone."""
		from tatva_connect.storage import file_manager

		key = frappe.db.get_value("File", {"file_url": ["like", "%download_file%"]}, "file_url")
		if not key:
			raise unittest.SkipTest("no offloaded file on this site")
		doc = frappe._dict({"file_url": key})
		url, expiry = file_manager.fetch_url(doc), file_manager.fetch_expires_at(doc)
		self.assertIn("sig=", url, "an off-site link must carry its own authorisation")
		self.assertIn("sp=r", url, "the link must be read-only")
		self.assertIn("sr=b", url, "the link must reach one blob, never the container")
		self.assertTrue(expiry, "a link that expires must say when")
		self.assertIn(expiry.replace(":", "%3A"), url, "the expiry must be the token's own, not a guess")


class TestOneProjectionOneDeclaration(IntegrationTestCase):
	"""The read side of a resource is built from its declaration, never a second list."""

	def test_the_file_list_selects_exactly_what_the_view_projects(self):
		from tatva_connect.api import partner_file as pf

		needed = {c for _key, cols, _resolve in pf._VIEW_FIELDS for c in cols}
		self.assertEqual(needed, set(pf._LIST_COLUMNS),
		                 "file_list selects a different column set than _file_view reads")

	def test_the_view_publishes_no_column_name_it_did_not_declare(self):
		from tatva_connect.api import partner_file as pf

		row = frappe._dict({c: None for c in pf._LIST_COLUMNS})
		self.assertEqual(set(pf._file_view(row)), {key for key, _c, _r in pf._VIEW_FIELDS})


class TestNoResponseNamesAnInternalTable(IntegrationTestCase):
	"""Drive the real projections and read what they actually say."""

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.set_user("Administrator")
		self.stage = frappe.db.get_value("CRM Lead Stage", {"name": ["like", "%::%"]}, "name")
		self.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Vocabulary Probe",
			"mobile_no": f"+9198127{int(frappe.generate_hash(length=8), 16) % 100000:05d}",
			"custom_vertical": VERTICAL, "custom_group": GROUP,
			"custom_substage": self.stage,
		}).insert(ignore_permissions=True)

	def _lead_payload(self):
		from tatva_connect.api.partner import _caller_fields, _curate

		_u, _mp, _is, parent_fields, child_allow = _caller_fields()
		return _curate(frappe.get_doc("CRM Lead", self.lead.name), parent_fields, child_allow)

	def test_a_page_and_a_single_read_agree_on_the_value(self):
		"""`lead_list` builds its rows with its own query, so it can resolve differently from `lead_get`
		and did — a page returned the composite key a single read had already turned into a label."""
		from tatva_connect.api.partner import _caller_fields, _curate, _readable

		_u, _mp, _is, parent_fields, child_allow = _caller_fields()
		doc = frappe.get_doc("CRM Lead", self.lead.name)
		single = _curate(doc, parent_fields, child_allow)
		paged = _readable("CRM Lead", {f: doc.get(f) for f in parent_fields if doc.get(f) is not None})
		for field, value in paged.items():
			self.assertEqual(value, single.get(field),
			                 f"a page and a single read disagree about {field}")
		self.assertNotIn("::", str(paged.get("custom_substage")), "the page must resolve the stage too")

	def test_a_user_link_stays_an_email(self):
		"""A composite key is meaningless to a caller, so it reads as its label. `lead_owner` is the
		opposite case: its key IS the identifier a caller matches on, and its title is a full name that
		is neither unique nor addressable. Resolving by "does the target have a title" swaps one for the
		other and silently breaks anyone integrating on it."""
		from tatva_connect.api.partner import _readable

		email = frappe.db.get_value("User", {"enabled": 1, "name": ["like", "%@%"]}, "name")
		out = _readable("CRM Lead", {"lead_owner": email, "custom_substage": self.stage})
		self.assertEqual(out["lead_owner"], email, "lead_owner must be published exactly as stored")
		self.assertNotIn("::", str(out["custom_substage"]), "a composite key must read as its label")
		self.assertNotEqual(out["custom_substage"], self.stage, "the stage was not resolved at all")

	def test_the_lead_payload_names_no_table(self):
		payload = self._lead_payload()
		self.assertTrue(payload.get("name"), "the projection returned nothing to assert on")
		self.assertEqual(table_names_in(payload), [])

	def test_a_lead_with_no_child_rows_is_the_same_shape_as_one_with_many(self):
		"""The variation that hid the leak: an empty child array passes every check a full one fails."""
		empty = self._lead_payload()
		child_field = next((k for k, v in empty.items() if isinstance(v, list)), None)
		if not child_field:
			raise unittest.SkipTest("this contract ticks no child section")
		self.assertEqual(empty[child_field], [], "expected the probe lead to start with no child rows")
		self.assertEqual(table_names_in(empty), [])

	def test_the_file_view_names_no_table(self):
		from tatva_connect.api import partner_file as pf

		for doctype in ("CRM Lead", *pf._TARGET_DOCTYPES):
			row = frappe._dict({c: None for c in pf._LIST_COLUMNS})
			row.attached_to_doctype = doctype
			row.file_url = "/private/files/probe.txt"
			self.assertEqual(table_names_in(pf._file_view(row)), [],
			                 f"a file homed on {doctype} reported its table name")
