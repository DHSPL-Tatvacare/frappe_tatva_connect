# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A stored phone number is a REAL number, or the save is refused.

The old shaper counted digits: ten got `+91` in front, anything else got a `+` and whatever was there. So
`09876543210` — what browser autofill hands you, and how most people write an Indian number — was stored as
`+09876543210`. That number matches no WhatsApp inbound, no telephony call, and no other spelling of the
same patient, so the same person could be created twice and the dedup index would not notice. `hello` was
stored as `hello`.

The shaping is Google's libphonenumber now, which Frappe already depends on and already wraps. It carries
every country's real numbering plan, so it is the thing that knows `1111111111` is not an Indian mobile and
that India drops a leading trunk `0`. There is no regex here to keep up to date.

What is asserted:

  * an Indian number in any spelling a person uses lands on the SAME stored value;
  * a number a rep PASTED lands there too — carrying the invisible bidi wrapper WhatsApp and iOS add, or
    the second country code the `Phone` picker's own prefix creates, or both at once;
  * the number as given is always read FIRST, so no spelling the library already accepts is ever rewritten;
  * a refusal names the number the rep can SEE, and is raised where `mute_messages` cannot eat its reason;
  * a foreign number is stored as its own country says and is NEVER rewritten to India;
  * a number that is not real anywhere is REFUSED on save, naming the field the rep is looking at;
  * the refusal is a WRITE rule — a partner searching by a malformed number gets no results, not a 500;
  * the default country is read from Frappe's System Settings, not named in code;
  * a caller that knows the country (an intake form with a picker) can say so, and it wins over the default.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.lead.test_phone_is_a_real_number
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.api import _base
from tatva_connect.whatsapp import phone as store

DEDUP_SWITCH = "Lead::CRM Lead::dedup"

# Every spelling of ONE Indian mobile a person actually types, including what autofill produces.
ONE_INDIAN_NUMBER = [
	"9876543210",
	"09876543210",
	"098765 43210",
	"+919876543210",
	"+91 98765 43210",
	"+91-98765-43210",
	"(+91) 9876543210",
	"  9876543210  ",
	"0091 9876543210",
]
STORED = "+919876543210"

# Real numbers that are not Indian. None of these may be rewritten to +91.
FOREIGN = {
	"+39 06 1234 5678": "+390612345678",      # Rome landline
	"+966 50 123 4567": "+966501234567",      # Saudi mobile
	"+1 415-555-2671": "+14155552671",        # US
	"+971 50 123 4567": "+971501234567",      # UAE mobile
	"+44 20 7946 0958": "+442079460958",      # London
}

# The SAME number again, as it arrives when a rep pastes rather than types. A `Phone` control stores
# `<isd>-<number>` and its picker has already supplied the code, so a pasted number brings a second one;
# WhatsApp and iOS add a bidi wrapper (U+202A) that occupies no space and survives `str.strip()`.
# Written as escapes on purpose: a literal U+202A in this file would be as invisible here as it is on a form.
PASTED = [
	"+91-\u202a9876543210",       # the picker's own code in front of a pasted number: the shape that fails
	"\u202a9876543210",           # pasted into an empty field
	"\u202b9876543210\u202c",     # the right-to-left wrapper and its terminator
	"\ufeff9876543210",           # a byte-order mark, which a spreadsheet paste carries
	"+91-+919876543210",          # pasted WITH its own country code
	"+91-+91 98765 43210",        # the same, spaced the way a person writes it
	"+91-91 98765 43210",         # the same again, with no second plus to mark the seam
	"+91-\u202a+919876543210",    # both defects at once
]

# Not a number anywhere on earth. `+91-+91hello` is here so a doubled code is never a licence to guess.
JUNK = ["hello", "12345", "1111111111", "+91987654321", "99999999999999999", "+91-+91hello"]


class TestPhoneIsARealNumber(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.set_user("Administrator")
		# In process, never written to the bench: a lead insert commits, so a written switch survives rollback and leaks into every later suite.
		enabled = patch("tatva_connect.automation.is_enabled",
						side_effect=lambda key: key == DEDUP_SWITCH)
		enabled.start()
		self.addCleanup(enabled.stop)

	def _lead(self, **kw):
		return frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "ZZ Phone Probe",
			"custom_vertical": "Goodflip-Care", "custom_group": "Anaya", **kw,
		})

	# ---- the shaping -----------------------------------------------------------------------------------

	def test_every_spelling_of_one_indian_number_stores_the_same(self):
		"""RED before the change for the three `0`-prefixed spellings, which became `+0…` and matched nothing."""
		for typed in ONE_INDIAN_NUMBER:
			with self.subTest(typed=typed):
				self.assertEqual(store.to_e164(typed), STORED,
								 f"{typed!r} did not land on the one stored form")

	def test_a_pasted_number_lands_on_the_same_stored_form(self):
		"""RED before the change: every one of these was refused. The invisible ones are the cruel half —
		the number printed back at the rep beside the word "invalid" reads exactly like a correct one."""
		for pasted in PASTED:
			with self.subTest(pasted=pasted):
				self.assertEqual(store.to_e164(pasted), STORED,
								 f"{pasted!r} did not land on the one stored form")

	def test_a_value_that_is_only_invisible_characters_is_blank(self):
		"""Nothing visible was given, so nothing was given — a blank, not a bad number."""
		self.assertEqual(store.to_e164("\u202a\u202c\ufeff"), "")

	def test_a_doubled_country_code_is_recovered_whatever_the_country(self):
		"""The seam is read off the value itself, so this is not an India rule wearing a general name."""
		self.assertEqual(store.to_e164("+1-+14155552671"), "+14155552671")
		self.assertEqual(store.to_e164("+966-+966501234567"), "+966501234567")

	def test_the_number_as_given_is_always_read_first(self):
		"""The invariant that protects every number that already works: a fallback spelling is REACHED only
		when the original does not parse, and ACCEPTED only when libphonenumber calls it valid. Without this
		ordering a real national number opening with its own country's digits would be silently cut."""
		for typed in (*ONE_INDIAN_NUMBER, *FOREIGN, *PASTED):
			with self.subTest(typed=typed):
				self.assertEqual(next(iter(store._spellings(typed))), typed,
								 "a rewriting was tried before the number the rep actually gave")

	def test_the_refusal_names_the_number_the_rep_can_see(self):
		"""Quoting the raw value printed a number that looks correct next to the word "invalid"."""
		with self.assertRaises(frappe.ValidationError) as caught:
			store.to_e164("\u202a12345")
		self.assertNotIn("\u202a", str(caught.exception),
						 "the refusal quoted a number carrying the very character it is refusing")
		self.assertIn("12345", str(caught.exception))

	def test_the_phone_is_shaped_before_the_fold_can_mute_the_reason(self):
		"""`_fold_submission_to_lead` mutes messages so no internal notice reaches a patient, and frappe
		raises WITHOUT recording the message under that flag (`utils/messages.py:61`) — so a refusal raised
		inside the fold arrives as an empty dialog and the rep is told nothing. Shaping is declared on
		`validate`, which frappe runs before the `after_insert` that folds, so the wording survives."""
		from tatva_connect import hooks

		wildcard = hooks.doc_events["*"]
		self.assertIn("tatva_connect.intake.intake.canonicalise_phones", wildcard["validate"],
					  "the shaping left the seam that runs before the mute")
		self.assertIn("tatva_connect.intake.intake.route_submission", wildcard["after_insert"])

	def test_a_foreign_number_keeps_its_own_country(self):
		"""The default country applies ONLY to a number with no `+`. A Saudi patient stays Saudi."""
		for typed, expected in FOREIGN.items():
			with self.subTest(typed=typed):
				self.assertEqual(store.to_e164(typed), expected,
								 f"{typed!r} was rewritten away from its own country")

	def test_a_number_that_is_not_real_is_refused(self):
		"""RED before the change: every one of these was stored as written."""
		for typed in JUNK:
			with self.subTest(typed=typed):
				with self.assertRaises(frappe.ValidationError):
					store.to_e164(typed)

	def test_blank_stays_blank(self):
		"""No number given is not a bad number — some leads are email or name only."""
		for empty in ("", None, "   "):
			self.assertEqual(store.to_e164(empty), "")

	def test_a_caller_that_knows_the_country_overrides_the_default(self):
		"""An intake form with a country picker hands the region down; a Saudi local number is then Saudi
		and not read against India."""
		self.assertEqual(store.to_e164("0501234567", region="SA"), "+966501234567")
		with self.assertRaises(frappe.ValidationError):
			store.to_e164("0501234567")  # same digits, no region -> not a valid Indian number

	def test_the_default_country_comes_from_frappe_not_from_code(self):
		"""So opening a second country is one operator setting, not an edit here."""
		country = frappe.db.get_single_value("System Settings", "country")
		self.assertEqual(store.region_default(),
						 (frappe.get_cached_value("Country", country, "code") or "").upper())

	# ---- the save --------------------------------------------------------------------------------------

	def test_the_lead_save_stores_the_shaped_number(self):
		lead = self._lead(mobile_no="09876543210").insert(ignore_permissions=True)
		self.assertEqual(lead.mobile_no, STORED,
						 "an autofilled number was stored in a form nothing can match")

	def test_the_lead_save_is_refused_and_names_the_field_the_rep_sees(self):
		"""The message says "Mobile No.", the label on screen — not `mobile_no`."""
		with self.assertRaises(frappe.ValidationError) as caught:
			self._lead(mobile_no="hello").insert(ignore_permissions=True)
		self.assertIn("Mobile No", str(caught.exception),
					  "the refusal did not name the field the rep is looking at")

	def test_every_declared_phone_field_is_shaped_not_just_mobile(self):
		lead = self._lead(mobile_no="9876543210",
						  custom_alternate_number="09000000011").insert(ignore_permissions=True)
		self.assertEqual(lead.custom_alternate_number, "+919000000011",
						 "a second phone field was left unshaped")

	# ---- a lookup is not a write -----------------------------------------------------------------------

	def test_searching_by_a_malformed_number_finds_nothing_and_does_not_raise(self):
		"""A partner asking for a lead by a bad number has an answer — no such lead. Answering with a 500
		would tell them their integration is broken when it is their query that is."""
		self.assertEqual(_base._norm_phone("hello"), "hello")
		self.assertEqual(_base._norm_phone("09876543210"), STORED,
						 "a lookup by an autofilled number should find the lead it means")
		for empty in (None, ""):
			self.assertEqual(_base._norm_phone(empty), empty)

	# ---- the picker's list ------------------------------------------------------------------------------

	def test_the_dial_code_list_is_frappes_countries_and_the_librarys_codes(self):
		"""No country table in our code: the names are Frappe's, the codes are libphonenumber's."""
		rows = store.dial_codes()
		by_region = {r["region"]: r for r in rows}
		self.assertEqual(by_region["IN"]["dial"], "+91")
		self.assertEqual(by_region["SA"]["dial"], "+966")
		self.assertEqual(by_region["US"]["dial"], "+1")
		self.assertEqual([r["region"] for r in rows if r["default"]], [store.region_default()],
						 "exactly one country must be marked the default, and it is the site's")
