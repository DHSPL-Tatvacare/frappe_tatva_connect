# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A profile row must never be able to destroy a message that already reached a patient.

THE PRODUCTION FAILURE. `frappe_whatsapp.WhatsAppMessage.before_insert` sends the message and THEN calls
`create_whatsapp_profile`, which is a read-then-write on a globally unique `number`: it asks whether a
profile exists and inserts one if not. Two sends to one number in the same moment both find it absent and
both insert; the second dies with `IntegrityError 1062 ... for key 'number'`. Because that happens inside
`before_insert`, it aborts OUR `WhatsApp Message` row — after the provider has already accepted the
message. Patient messaged, nothing recorded, and the only surviving copy is whatever the provider's echo
webhook writes later.

WHAT THIS LOCKS. `_claim_whatsapp_profile` runs before `super().before_insert()` and claims the row with
INSERT IGNORE, so upstream's `exists` finds it and its unguarded insert never executes. The property is
therefore not "we catch the error" but "upstream never writes", which is why the green test asserts a
clean run AND exactly one row.

NOTHING IS SENT HERE. The two methods are driven directly on an un-inserted document, so no adapter, no
HTTP and no `before_insert` chain runs — this suite cannot message anybody.

RED IS PROVEN, NOT ASSUMED, AND ON ONE CONDITION: the upstream method through the same forced race must
raise a DUPLICATE-ENTRY error naming `number`. Not "raised something" — a disjunction is how an earlier
fix in this class went green while the real bug kept firing.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.whatsapp.test_profile_claim_race
"""
import threading

import frappe
from frappe.tests.utils import FrappeTestCase

_ACCOUNT = "ZZ Profile Claim Race"
_TENANT = "https://live-mt-server.example/ZZprofileclaim"
_NUMBER = "919000000042"  # not a real number; the unique key under test
PROFILE_DT = "WhatsApp Profiles"


def _message_doc():
	"""An un-inserted outgoing row — enough for the two methods under test, and nothing else."""
	return frappe.get_doc({
		"doctype": "WhatsApp Message", "type": "Outgoing", "to": _NUMBER,
		"profile_name": "Claim Race", "whatsapp_account": _ACCOUNT, "message": "x",
	})


class _ProfileRaceBase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("WhatsApp Account", _ACCOUNT):
			frappe.get_doc({
				"doctype": "WhatsApp Account", "account_name": _ACCOUNT, "status": "Active",
				"url": _TENANT, "token": "profile-claim-test-token", "custom_provider": "WATI",
				"custom_wati_channel_number": "919000000001",
			}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input
		frappe.db.commit()

	def setUp(self):
		super().setUp()
		self._wipe()

	def tearDown(self):
		self._wipe()
		super().tearDown()

	def _wipe(self):
		frappe.db.delete(PROFILE_DT, {"number": _NUMBER})
		frappe.db.commit()

	def _rows(self):
		return frappe.db.count(PROFILE_DT, {"number": _NUMBER})

	def _race(self, body):
		"""Two real connections meeting at a barrier — frappe.local is thread-local, so a thread without
		its own connection would share this test's transaction and race nothing."""
		site, raised = frappe.local.site, []
		barrier = threading.Barrier(2, timeout=10)

		def run():
			frappe.init(site=site)
			frappe.connect()
			try:
				frappe.db.begin()
				body(barrier)
				frappe.db.commit()
			except Exception as e:  # collected, never swallowed — a thread that died quietly proves nothing
				raised.append(e)
			finally:
				frappe.destroy()

		threads = [threading.Thread(target=run) for _ in range(2)]
		for t in threads:
			t.start()
		for t in threads:
			t.join(timeout=60)
		# This connection's read view predates what the threads committed — refresh it, or the count below
		# reads zero and the cleanup DELETE raises 1020 against rows it cannot see.
		frappe.db.commit()
		return raised


class TestUpstreamProfileCreateRaces(_ProfileRaceBase):
	"""THE PREMISE. Upstream's own method, driven through the forced race. If this ever stops raising,
	the bug being fixed is not the bug production reported and this file is worthless."""

	def test_upstream_create_raises_duplicate_entry_on_the_number_key(self):
		def upstream(barrier):
			doc = _message_doc()
			frappe.db.exists(PROFILE_DT, {"number": _NUMBER})  # the read half of the read-then-write
			barrier.wait()
			doc.create_whatsapp_profile()

		raised = self._race(upstream)
		self.assertEqual(len(raised), 1, f"exactly one writer must lose the race; got {raised!r}")
		self.assertIn("number", str(raised[0]), "the collision must be on the `number` unique key")
		self.assertIn("1062", str(raised[0]), f"production reports a duplicate entry; this raised {raised[0]!r}")


class TestClaimedProfileSurvives(_ProfileRaceBase):
	"""THE FIX, through the real override method, then upstream's own — which must find the row and do
	nothing rather than insert a second one."""

	def test_two_concurrent_sends_both_survive_and_leave_one_profile(self):
		def claimed(barrier):
			doc = _message_doc()
			# The barrier goes BEFORE the claim, not after: the claim takes a lock on the unique key that
			# is held to commit, so two threads claiming first would each be waiting for the other to reach
			# a barrier neither can reach. Meeting first is also the real shape — two sends arriving at once.
			barrier.wait()
			doc._claim_whatsapp_profile()
			doc.create_whatsapp_profile()  # upstream's, unchanged — it must now be a no-op

		raised = self._race(claimed)
		self.assertEqual(raised, [], f"a claimed profile still raised: {raised!r}")
		self.assertEqual(self._rows(), 1, "the profile must exist exactly once — not zero, not twice")

	def test_the_claim_writes_the_same_row_upstream_would_have(self):
		"""Same key, same fields — nothing downstream may be able to tell who wrote it."""
		_message_doc()._claim_whatsapp_profile()
		frappe.db.commit()
		row = frappe.db.get_value(
			PROFILE_DT, {"number": _NUMBER},
			["profile_name", "number", "whatsapp_account", "title"], as_dict=True,
		)
		self.assertEqual(row.profile_name, "Claim Race")
		self.assertEqual(row.number, _NUMBER)
		self.assertEqual(row.whatsapp_account, _ACCOUNT)
		self.assertEqual(row.title, f"Claim Race - {_NUMBER}", "title must read as upstream's set_title builds it")
