# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The CI lock for what a partner is TOLD when a call fails.

`api-docs/pages/partner-errors.mdx` promises that a stack trace or a raw framework error is never
returned. Three sites made that a lie — a Python class name in `error_summary`, a requests exception
name in an attach refusal, a json parser's own text as a per-record error — and two more put a Desk
dialog ("open the task", "mark it Done from the Tasks list") in an HTTP 400 body, where no task list
exists. Each was correct code with the wrong words, so nothing caught them.

This is a pure-AST source lock in the same shape as `test_no_perm_bypass.py`: it reads the source, not
the running app, and fails the build when a partner-reachable message breaks one of four rules:

  * it names a Python exception class      (a leak — the caller cannot act on our class names). Two
                                            shapes, because the leak had two: a class name written
                                            into the prose, and `type(e).__name__` interpolated into
                                            it at runtime, which no scan of the literal can see.
  * it instructs a Desk action             (wrong audience — an HTTP caller has no form and no button)
  * it blames the caller                   ("invalid", "incorrect", "illegal" — NN/g and Microsoft both
                                             prohibit these)
  * it addresses the caller in the second person  (a HOUSE register rule, not an industry one — the
                                             whole repo reads third person and consistency is the point)

WHAT IS PARTNER-REACHABLE, and why it is two sets. Every `_()` string in the partner API modules is,
because those modules exist to answer a partner. Elsewhere in the app a rule guards the Desk AND the
API from one throw, and only its API arm is in scope — that arm is always the second argument of
`_base.throw_by_audience`, the ONE audience seam, so it is found by name rather than by guessing which
shared module might be on a partner's path.

The lock is a guard, not a framework. It does not judge whether a message ends in a next step; that is
a review question. It catches the four things that are never right.
"""
import ast
import os
import re

try:  # This is a pure-AST source lock — it runs in the bench AND standalone (tcsec, no frappe).
	from frappe.tests.utils import FrappeTestCase
except ModuleNotFoundError:
	import unittest

	FrappeTestCase = unittest.TestCase

from tatva_connect.tests.static._lock_helpers import app_root

_APP_DIR = app_root(__file__)

# The modules whose whole job is answering a partner. Every `_()` string in these is on the wire.
_PARTNER_MODULES = (
	"_base.py", "partner.py", "partner_activity.py", "partner_bulk_job.py",
	"partner_bulk_worker.py", "partner_call.py", "partner_file.py", "partner_note.py",
)

# The ONE audience seam (`_base.throw_by_audience`). Its SECOND argument is the API wording; the first
# is the Desk wording and is deliberately exempt — a rep really can open the task and tick the box.
_SEAM = "throw_by_audience"

# A Python class name on the wire. CamelCase ending in Error/Exception, which is what leaked every time
# (`ConnectTimeout` was reached through `type(e).__name__`, `JSONDecodeError` through `str(exc)`).
_EXC_CLASS = re.compile(r"\b[A-Z][A-Za-z0-9]*(?:Error|Exception)\b")

# Desk instructions. Small and literal on purpose: a denylist that guesses goes red on innocent prose.
_DESK_PHRASES = ("open the task", "tasks list", "fill its form", "click ", "from the list")

_BLAME = re.compile(r"\b(invalid|incorrect|illegal)\b", re.I)
_SECOND_PERSON = re.compile(r"\byou(?:r|rs|'re)?\b", re.I)


def _violations(text):
	"""The rules this message breaks, as a list of reasons. Empty means it passes."""
	found = []
	leak = _EXC_CLASS.search(text)
	if leak:
		found.append(f"names the Python class {leak.group(0)!r}")
	for phrase in _DESK_PHRASES:
		if phrase in text.lower():
			found.append(f"instructs a Desk action ({phrase.strip()!r})")
	blame = _BLAME.search(text)
	if blame:
		found.append(f"blames the caller ({blame.group(0).lower()!r})")
	person = _SECOND_PERSON.search(text)
	if person:
		found.append(f"uses the second person ({person.group(0).lower()!r})")
	return found


def _translated_strings(node):
	"""Every string literal this subtree passes to `_()` — the frappe translation call every
	user-facing string in this app goes through. Read from the AST, so a docstring, a comment and a
	schema paragraph (none of which are translated) never reach the rules."""
	for sub in ast.walk(node):
		if not isinstance(sub, ast.Call):
			continue
		if not (isinstance(sub.func, ast.Name) and sub.func.id == "_"):
			continue
		if sub.args and isinstance(sub.args[0], ast.Constant) and isinstance(sub.args[0].value, str):
			yield sub.args[0].lineno, sub.args[0].value


def _class_name_reads(tree):
	"""Every `type(...).__name__` in this module, as linenos.

	This is the leak the literal scan CANNOT see: the prose reads `could not be fetched: {0}` and the
	class name arrives at runtime. A module whose job is answering a partner has no honest use for its
	own class names, so the shape is refused outright rather than traced to a message."""
	for sub in ast.walk(tree):
		if not isinstance(sub, ast.Attribute) or sub.attr != "__name__":
			continue
		inner = sub.value
		if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name) and inner.func.id == "type":
			yield sub.lineno


def _seam_api_strings(tree):
	"""The API arm of every audience seam call: the strings inside `throw_by_audience(desk, API, ...)`."""
	for sub in ast.walk(tree):
		if not isinstance(sub, ast.Call):
			continue
		fn = sub.func
		name = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else None
		if name == _SEAM and len(sub.args) >= 2:
			yield from _translated_strings(sub.args[1])


def _python_files():
	for root, _dirs, files in os.walk(_APP_DIR):
		if "__pycache__" in root or os.sep + "tests" in root:
			continue
		for name in files:
			if name.endswith(".py"):
				yield os.path.join(root, name)


def _scan():
	"""Return [(relpath, lineno, reason, message), ...] for every partner-reachable message that breaks
	a rule."""
	violations = []
	for path in _python_files():
		with open(path, encoding="utf-8") as fh:
			source = fh.read()
		tree = ast.parse(source, filename=path)
		in_partner_module = (
			os.path.basename(path) in _PARTNER_MODULES
			and os.path.basename(os.path.dirname(path)) == "api"
		)
		rel = os.path.relpath(path, _APP_DIR)
		strings = _translated_strings(tree) if in_partner_module else _seam_api_strings(tree)
		for lineno, text in strings:
			for reason in _violations(text):
				violations.append((rel, lineno, reason, text))
		if in_partner_module:
			for lineno in _class_name_reads(tree):
				violations.append((rel, lineno, "reads a Python class name (`type(...).__name__`)",
				                   "<interpolated at runtime>"))
	return violations


class TestPartnerMessageQuality(FrappeTestCase):
	def test_no_partner_message_leaks_blames_or_instructs_the_desk(self):
		violations = _scan()
		if violations:
			report = "\n".join(
				f"  {path}:{lineno} — {reason}\n      {text!r}"
				for path, lineno, reason, text in violations
			)
			self.fail(
				f"{len(violations)} partner-reachable message(s) break the error-message rule "
				f"(<what happened, with the real values>. <what to do, imperative>.):\n{report}"
			)

	def test_the_scanner_catches_each_of_the_four_rules(self):
		# Self-check: one planted message per rule, each caught for the right reason. Every string here
		# is one this API really shipped before this lock existed.
		self.assertIn("Python class", _violations("A JSONDecodeError was raised.")[0])
		self.assertIn("Desk action", _violations("Mark it Done from the Tasks list or open the task.")[0])
		self.assertIn("blames", _violations("Invalid or missing API key.")[0])
		self.assertIn("second person", _violations("Capture your location to complete this visit.")[0])

	def test_the_scanner_catches_the_interpolated_class_name(self):
		# The leak the literal scan cannot see: `_("... {0}").format(type(e).__name__)` shipped
		# `ConnectTimeout` and `PayloadRejected` to partners while the literal stayed innocent.
		tree = ast.parse('throw_field(_("could not be fetched: {0}").format(type(e).__name__), ["file_url"])')
		self.assertEqual(list(_class_name_reads(tree)), [1])
		self.assertEqual(list(_class_name_reads(ast.parse("_classify(e, fn.__name__)"))), [],
		                 "a plain __name__ read is not a class-name leak")

	def test_a_clean_message_passes(self):
		self.assertEqual(_violations(
			"`direction` reads `Sideways` and a call is either Inbound or Outbound. Send one of those "
			"two values."
		), [])

	def test_only_translated_strings_are_judged(self):
		# A docstring, a comment and an untranslated literal are not messages: they must not be judged,
		# or the lock goes red on prose that no partner ever sees.
		tree = ast.parse(
			'"""A ValidationError is raised when you click Save."""\n'
			'NOTE = "invalid, per your click on the Tasks list"  # ValidationError\n'
			'x = _("Send `mobile_no` in E.164.")\n'
		)
		self.assertEqual([text for _lineno, text in _translated_strings(tree)],
		                 ["Send `mobile_no` in E.164."])

	def test_the_seam_judges_the_api_arm_and_spares_the_desk_arm(self):
		# The Desk wording keeps its Desk instruction; only the second argument is on the wire.
		tree = ast.parse(
			'throw_by_audience(\n'
			'    _("Mark it Done from the Tasks list or open the task."),\n'
			'    _("Coordinates cannot be sent over the API. Leave `status` as it is."),\n'
			'    ["status"],\n'
			')\n'
		)
		self.assertEqual([text for _lineno, text in _seam_api_strings(tree)],
		                 ["Coordinates cannot be sent over the API. Leave `status` as it is."])
