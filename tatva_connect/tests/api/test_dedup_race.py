# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""THE DEDUP RACE, AND WHY THE ABSORB HAS TO READ WITH A LOCK.

Two messages from one new patient arrived a second apart on live traffic. Two workers each looked for
a lead, each saw none, and each inserted. The unique index did its job and refused the second — and
`_upsert_one` is built for exactly that: roll back to the savepoint, look the winner up, merge onto it.

It looked the winner up and did not find it. InnoDB's REPEATABLE READ answers a plain SELECT from the
snapshot this transaction took BEFORE the other worker committed, so the index could prove the row
existed while the very next read said it did not. The absorb was skipped, the insert re-raised, and the
patient's second message was dropped with it. `for_update` is what makes that read see the latest
commit instead of the snapshot.

WHAT THE FAKE HERE IS. It is not a stand-in for `_upsert_one` — that runs for real, inserts for real,
and collides with a real unique index. It stands in for the DATABASE'S ISOLATION LEVEL, which a
single-connection test cannot otherwise produce: a non-locking read of the anchor answers None (the
stale snapshot), a locking read answers the truth. Both halves are the documented behaviour of the
engine this ships on.

On the code before the fix this fails: the recovery read carried no lock, so it took the None, and
`_upsert_one` raised `UniqueValidationError` at the caller instead of returning the lead.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.api.test_dedup_race
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.api import partner

_VERTICAL, _GROUP = "_TC Race Line", "_TC Race Group"
_NUMBER = "+919000000731"


class TestTheDedupRaceIsAbsorbed(FrappeTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		for dt, field, name in (("CRM Vertical", "vertical_name", _VERTICAL), ("CRM Group", "group_name", _GROUP)):
			if not frappe.db.exists(dt, name):
				frappe.get_doc({"doctype": dt, field: name}).insert(
					ignore_permissions=True, ignore_if_duplicate=True
				)
		# The lead the OTHER worker created and committed a moment earlier.
		self.winner = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Race Winner", "mobile_no": _NUMBER,
			"custom_vertical": _VERTICAL, "custom_group": _GROUP,
		}).insert(ignore_permissions=True)

	def _upsert(self):
		"""The same call `whatsapp.enrol._create` makes, with the same descriptor shape."""
		return partner._upsert_one(
			{"mobile_no": _NUMBER, "first_name": "Second Message"},
			frappe._dict(source="WhatsApp", vertical=_VERTICAL, crm_group=_GROUP, program=None),
			False,
			["mobile_no", "first_name"],
			{},
			allowed_programs=[],
		)

	def test_a_lead_another_worker_just_committed_is_absorbed_not_raised(self):
		"""The whole defect in one call: the snapshot hides the winner, the index finds it."""
		real = frappe.db.get_value

		def snapshot_bound(doctype, filters=None, fieldname="name", *args, **kwargs):
			"""REPEATABLE READ: this anchor is invisible to a plain read, visible to a locking one."""
			if doctype == "CRM Lead" and isinstance(filters, dict) and filters.get("mobile_no") == _NUMBER:
				if not kwargs.get("for_update"):
					return None
			return real(doctype, filters, fieldname, *args, **kwargs)

		with patch.object(frappe.db, "get_value", side_effect=snapshot_bound):
			doc, action = self._upsert()

		self.assertEqual(action, "updated", "the racing message minted a second lead instead of joining")
		self.assertEqual(doc.name, self.winner.name, "the message joined a different lead than the winner")

	def test_a_clash_on_another_unique_key_is_still_raised(self):
		"""The absorb must stay narrow: only OUR anchor is folded onto. A duplicate on any other unique
		key is somebody else's problem and must not be swallowed as a successful upsert."""
		real = frappe.db.get_value

		def never_found(doctype, filters=None, fieldname="name", *args, **kwargs):
			if doctype == "CRM Lead" and isinstance(filters, dict) and filters.get("mobile_no") == _NUMBER:
				return None
			return real(doctype, filters, fieldname, *args, **kwargs)

		with patch.object(frappe.db, "get_value", side_effect=never_found):
			with self.assertRaises((frappe.UniqueValidationError, frappe.DuplicateEntryError)):
				self._upsert()
