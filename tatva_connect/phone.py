# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A phone number has THREE jobs in this app, and they must never be confused.

  MATCH  `phone.match_digits` (here)        digits, to compare two spellings of one number
  STORE  `whatsapp.phone.to_e164`           the canonical `+91…` a row is saved as
  SEND   `Declaration.conform_number`       what THIS provider will accept on the wire

They live apart on purpose. STORE produces `+919059067237`; WATI REJECTS that, because its sends put the
number into a URL where `+` decodes as a space. Merging store and send is how a patient's message reaches
a stranger in another country a second time, in a nicer wrapper.

MATCH is not an address and must never be used as one. It flattens `+91-7753022190` and a bare
`9059067237` to digits, so two numbers a provider would DIAL DIFFERENTLY compare equal here. That is
correct for lining an inbound event up against a stored number, and catastrophic on a send.

This module sits above both domains because both call it: WhatsApp matches an inbound `waId` against a
lead, telephony matches a provider's call record against a DID. Putting it in either would make one
import the other to shape a number.

THE WORD "normalize" IS BANNED FOR PHONE NUMBERS. Three functions were called `normalize_number` /
`phone_digits`; one of them claimed E.164 in its docstring and returned bare digits, and every caller
believed the docstring. That is the sentence that cost a real misdelivery on 2026-07-21.
"""
import re

_NON_DIGIT = re.compile(r"\D")


def match_digits(value, last=None) -> str:
	"""The comparable key for a phone number: its digits, optionally only the last `n`.

	`last` is what the telephony envelope needs — a provider reports the same subscriber as `9911232686`
	and `+919911232686`, so the last ten digits are the only part guaranteed to agree. It is an ARGUMENT
	rather than a third function, because "compare two spellings" is one job however many digits it keeps.

	A value with fewer than `last` digits is not a full number and comes back empty, never truncated: a
	partial key would match the wrong record rather than no record.
	"""
	digits = _NON_DIGIT.sub("", str(value or ""))
	if last is None:
		return digits
	return digits[-last:] if len(digits) >= last else ""
