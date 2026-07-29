# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""ONE DECLARATION OF HOW A VALUE IS FILLED — `Literal` · `From Context` · `Expression`.

W5.4. The vocabulary existed twice and agreed only by coincidence:

    contract.FROM_CONTEXT = "From Context"      a constant, read by `sends._template_parameters`
    actions.py:379         == "From Context"    the Set Field runtime, comparing a TYPED string
    actions.py:766         ["Literal", …]       the Select the author picks from, typed again
    actions.py:1012        else: # From Context the Create Task due-date runtime, typed again

Nothing failed if one drifted, and that is the whole problem. Rename the option an author picks and the
runtime's `==` stops matching it — then Set Field falls through to its literal branch and writes the
author's VARIABLE NAME onto the field, and Create Task silently takes its default due date. Both are
silent on a live patient record, which is the failure class this engine removed everywhere else.

WHY `refs.py` IS THE HOME, and it is not an arbitrary pick. `refs` is titled "the value contract: every
value carries where it came from", and `Literal` vs `From Context` is exactly that distinction — a value
the author typed, or a value that names run state. It is also the only module BOTH layers already import
at module scope (`actions.py:29`, `contract.py:39`), which is what dissolves the import cycle the earlier
note called the obstacle: `contract` → `registry` → `actions` is a cycle, but neither reaches `refs`.

HOW THIS LOCKS IT, and why it is not a string-matching test. The declaration is PATCHED and the runtime
must follow it. A runtime comparing its own typed copy cannot follow a renamed constant, so it fails —
which is precisely the drift that would otherwise ship silently. Asserting that two spellings match would
let both sides drift together and prove nothing (the same reasoning as
`test_registry_conformance.test_a_value_rows_field_ships_its_modes_from_the_contract`, which already
closed the FRONTEND copy of this same vocabulary).

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.workflow_engine.tests.test_one_value_mode_word
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import actions
from tatva_connect.workflow_engine import refs

_RENAMED = "ZZ Renamed Mode"


class TestTheRuntimeFollowsTheDeclaration(FrappeTestCase):
	"""THE red. Patch the one declaration; a runtime holding its own copy cannot follow."""

	def test_set_field_reads_context_through_the_declaration(self):
		"""`_resolve_set_field_value`'s From Context branch. With its own typed copy it falls through to
		the literal branch and writes the variable NAME onto the patient's field."""
		action = frappe._dict(value_mode=_RENAMED, context_field="patient_id", value="the-literal")
		with patch.object(refs, "FROM_CONTEXT", _RENAMED):
			resolved = actions._resolve_set_field_value(action, {"patient_id": "the-context-value"})
		self.assertEqual(resolved, "the-context-value",
		                 "Set Field did not follow the renamed declaration — it wrote its literal instead")

	def test_set_field_resolves_an_expression_through_the_declaration(self):
		action = frappe._dict(value_mode=_RENAMED, expression="1 + 1", value="the-literal")
		with patch.object(refs, "EXPRESSION", _RENAMED):
			resolved = actions._resolve_set_field_value(action, {})
		self.assertEqual(resolved, 2, "Set Field did not follow the renamed Expression declaration")

	def test_create_tasks_due_date_follows_the_declaration(self):
		"""`_due_at` tests only for Expression and lets From Context be the `else`, so the drift here is
		one-way and silent: a renamed Expression stops matching, the else branch reads a `due_from` that
		an Expression-mode action never set, and the task quietly takes its DEFAULT due date."""
		action = frappe._dict(due_mode=_RENAMED, due_expression='ctx["d"]', due_from=None)
		with patch.object(refs, "EXPRESSION", _RENAMED):
			resolved = actions._due_at(action, {"d": "2026-07-30 09:00:00"})
		self.assertIsNotNone(resolved, "Create Task did not follow the renamed declaration — the task fell "
		                     "through to its default due date")
		self.assertEqual(frappe.utils.get_datetime(resolved),
		                 frappe.utils.get_datetime("2026-07-30 09:00:00"))


class TestWhatAnAuthorIsOfferedIsTheDeclarationItself(FrappeTestCase):
	"""The other half: the Select the author picks from must BE the declared words, not a typed pair that
	happens to match. Rename the constant and a hardcoded options list goes red here."""

	def _mode_params(self):
		found = [
			(verb, param)
			for verb, spec in actions.VERBS.items()
			for param in spec.get("params") or []
			if param.get("name", "").endswith("_mode") and param.get("options")
		]
		self.assertTrue(found, "no mode param is declared — this lock would pass vacuously")
		return found

	def test_every_offered_mode_is_a_declared_one(self):
		declared = {refs.LITERAL, refs.FROM_CONTEXT, refs.EXPRESSION}
		for verb, param in self._mode_params():
			with self.subTest(verb=verb, param=param["name"]):
				self.assertLessEqual(
					set(param["options"]), declared,
					f"{verb}.{param['name']} offers a mode nothing declares: {param['options']}",
				)

	def test_the_gate_a_mode_opens_names_a_declared_mode(self):
		"""`depends_on_value` decides which box the author sees. A stale word there hides the control for a
		mode that is still live, which reads as 'the field vanished' rather than as a bug."""
		declared = {refs.LITERAL, refs.FROM_CONTEXT, refs.EXPRESSION}
		for verb, spec in actions.VERBS.items():
			for param in spec.get("params") or []:
				gate = param.get("depends_on_value") or {}
				for field, values in gate.items():
					if not field.endswith("_mode"):
						continue
					with self.subTest(verb=verb, param=param["name"]):
						self.assertLessEqual(set(values), declared,
						                     f"{verb}.{param['name']} is gated on an undeclared mode: {values}")


class TestTheDeclarationLivesInExactlyOnePlace(FrappeTestCase):
	"""A constant that is re-stated anywhere is a second declaration whatever it is called."""

	def test_the_contract_does_not_keep_its_own_copy(self):
		"""`contract.py` held these before W5.4. It may still EXPOSE them — `sends` and `registry` read
		`contract.FROM_CONTEXT` — but exposing must be the same object, never a second string."""
		from tatva_connect.workflow_engine import contract

		self.assertIs(contract.FROM_CONTEXT, refs.FROM_CONTEXT)
		self.assertIs(contract.LITERAL, refs.LITERAL)

	def test_no_module_types_the_vocabulary_a_second_time(self):
		"""A source scan, deliberately narrow: only the two words that are unambiguous as literals.
		`"Expression"` is skipped because it is also a legitimate FIELD LABEL (`actions.py`'s Small Text
		box is labelled Expression), and a scan that cannot tell a label from a mode would be noise."""
		import pathlib
		import re

		root = pathlib.Path(actions.__file__).resolve().parents[1]
		home = pathlib.Path(refs.__file__).resolve()
		pattern = re.compile(r"""["'](Literal|From Context)["']""")
		offenders = []
		for path in root.rglob("*.py"):
			if path.resolve() == home or "/tests/" in path.as_posix() or "__pycache__" in path.as_posix():
				continue
			for number, line in enumerate(path.read_text().splitlines(), 1):
				if pattern.search(line) and not line.lstrip().startswith("#"):
					offenders.append(f"{path.relative_to(root)}:{number}: {line.strip()}")
		self.assertEqual(offenders, [], "the value-mode vocabulary is typed outside its one declaration:\n"
		                 + "\n".join(offenders))
