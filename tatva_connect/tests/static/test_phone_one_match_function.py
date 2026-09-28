# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""THREE PHONE JOBS, THREE FUNCTIONS, AND THE WORD "normalize" IS BANNED.

Six functions shaped a phone number; they were only ever three jobs, and one job had three functions
doing byte-identical work in three modules.

  MATCH  `phone.match_digits`                  digits, to compare two spellings of one number
  STORE  `whatsapp.phone.to_e164`              the canonical `+91…` a row is saved as
  SEND   `Declaration.conform_number`          what THIS provider accepts on the wire

STORE and SEND must never merge. The stored form `+919876543210` is exactly what WATI rejects — its
sends put the number in a URL where `+` decodes as a space. Merging them is how a wrong-country send happens again in
a nicer wrapper.

MATCH must never be used as an address. It flattens `+91-7753022190` and a bare `9876543210` to the same
digits, which is precisely the ambiguity that let a provider guess a country.

THE WORD IS BANNED BECAUSE THE WORD IS WHAT HID THE BUG. `normalize_number` claimed E.164 in its
docstring and produced bare digits; everything downstream believed the docstring. Three names that cannot
be confused beat one name that has to be read carefully.

THE LANE. This runs with NO bench, so nothing here may import a module that reaches frappe.
`tatva_connect.phone` is frappe-free on purpose, which is what lets MATCH be asserted here at all; STORE
and SEND both reach frappe, so the one test that holds them apart lives in the bench lane, in
`tests/whatsapp/test_channel_contract.py`.
"""
import ast
import pathlib
import unittest

from tatva_connect import phone


class TestTheOneMatchFunction(unittest.TestCase):
	def test_it_reduces_every_spelling_of_one_number_to_the_same_key(self):
		"""The whole job: two spellings of one subscriber must compare equal."""
		self.assertEqual(phone.match_digits("+91-9911232686"), "919911232686")
		self.assertEqual(phone.match_digits("+91 99112 32686"), "919911232686")

	def test_last_keeps_the_subscriber_digits_the_telephony_envelope_needs(self):
		"""`last` replaces `envelope.phone_digits` — an ARGUMENT, not a third function."""
		self.assertEqual(phone.match_digits("+919911232686", last=10), "9911232686")
		self.assertEqual(phone.match_digits("9911232686", last=10), "9911232686")

	def test_a_number_too_short_to_be_one_yields_nothing_under_last(self):
		"""`phone_digits('55')` returned '' rather than '55', and callers rely on that."""
		self.assertEqual(phone.match_digits("55", last=10), "")

	def test_blank_input_is_empty_never_an_exception(self):
		for value in (None, "", "   "):
			self.assertEqual(phone.match_digits(value), "")


class TestTheThreeJobsStaySeparate(unittest.TestCase):
	"""G5 — and the one merge that would recreate the outage."""

	# `test_store_and_send_disagree_on_purpose` lives in tests/whatsapp/test_channel_contract.py: STORE and SEND both reach frappe, and this lane has no bench.

	def test_match_is_not_an_address(self):
		"""MATCH flattens the ambiguity SEND exists to refuse. Proven, so nobody 'simplifies' one into
		the other: the same match key comes from two numbers a provider would dial differently."""
		self.assertEqual(phone.match_digits("9876543210"), phone.match_digits("+9876543210"))


class TestTheWordIsGone(unittest.TestCase):
	"""B12 — matched against the DECLARED surface (every def in the app), not a remembered list of files."""

	_APP = pathlib.Path(__file__).resolve().parents[2]
	# `to_e164` and `conform_number` are the other two jobs and are allowed to exist; nothing else may shape a phone number.
	_BANNED = ("normalize_number", "phone_digits")

	def _phone_shaping_defs(self):
		found = []
		for path in self._APP.rglob("*.py"):
			rel = str(path.relative_to(self._APP.parent))
			if "/tests/" in rel or "/.archive/" in rel:
				continue
			for node in ast.walk(ast.parse(path.read_text())):
				if isinstance(node, ast.FunctionDef) and node.name in self._BANNED:
					found.append(f"{rel}:{node.lineno}:{node.name}")
		return found

	def test_no_function_in_the_app_is_named_for_the_word_that_hid_the_bug(self):
		found = self._phone_shaping_defs()
		self.assertEqual(
			found, [],
			f"these still shape a phone number under a banned name: {found}. The word `normalize` claimed "
			"E.164 and produced bare digits, and every caller believed the docstring.",
		)

	def test_the_match_function_lives_outside_both_domains_that_call_it(self):
		"""It is called by WhatsApp AND telephony, so it belongs to neither — otherwise one domain imports
		the other to shape a number."""
		self.assertEqual(phone.__name__, "tatva_connect.phone")
