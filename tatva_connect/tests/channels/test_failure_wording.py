# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""One failure, worded one way, wherever it was caught.

THE DRIFT THIS LOCKS OUT, measured on prod. The same provider refusal — WhatsApp `131026` — read
`131026 · Message undeliverable` when it arrived as a status event and `Message undeliverable` when it
was caught at send, because the two surfaces each built their own sentence. A report grouping by reason
split one failure into two buckets that could not be reconciled, and 96 patients who never received a
message were counted twice over as two different problems.

The wording lives in `channels.failure`; WHERE it is stored still differs per surface, and that is
deliberate — see the module docstring.
"""
import unittest

from tatva_connect.channels import failure


class TestFailureWording(unittest.TestCase):
	def test_a_provider_that_gave_both_halves_reads_as_both(self):
		self.assertEqual(failure.reason("131026", "Message undeliverable"), "131026 · Message undeliverable")

	def test_a_provider_that_gave_only_a_sentence_reads_as_that_sentence(self):
		"""No send envelope this app has observed carries a code, so this is the send path's normal shape —
		not a degraded one, and it must not gain an empty separator."""
		self.assertEqual(failure.reason(detail="Message undeliverable"), "Message undeliverable")

	def test_a_code_alone_is_still_a_reason(self):
		self.assertEqual(failure.reason("131026"), "131026")

	def test_our_own_words_are_used_only_when_the_provider_gave_none(self):
		"""A provider's own sentence always wins: ours is a placeholder, theirs is evidence."""
		self.assertEqual(failure.reason(fallback="ours"), "ours")
		self.assertEqual(failure.reason(detail="theirs", fallback="ours"), "theirs")

	def test_nothing_at_all_is_none_and_never_an_empty_string(self):
		"""An empty string in the column reads as "a failure with a blank reason", which is a different
		and worse claim than "no reason was recorded"."""
		self.assertIsNone(failure.reason())
		self.assertIsNone(failure.reason("", ""))
		self.assertIsNone(failure.reason(None, None))

	def test_a_provider_answering_with_a_page_is_still_answering_one_question(self):
		self.assertEqual(len(failure.reason(detail="x" * 900)), failure.MAX_LENGTH)

	def test_whitespace_a_provider_sent_is_not_a_reason(self):
		self.assertIsNone(failure.reason(detail="   "))
