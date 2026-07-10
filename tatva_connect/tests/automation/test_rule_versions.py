# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""`automation.versions` — the immutable, content-addressed rule definition every execution binds to.

The load-bearing assertions, in order of how much breaks without them:

  1. A frozen action carries its child-row `name`. That name is the identity `prefix_matches` compares;
     without it the whole migration contract collapses into guesswork.
  2. The hash covers the PROGRAM (criteria + ordered actions) and nothing else — so an idempotent save,
     or a description edit, or an enable/disable, mints no version.
  3. Reordering changes the hash. If `build_payload` ever sorted its action list, the engine would think
     a reorder were a no-op and silently re-route every parked lead.
  4. A version's definition cannot be edited afterwards.
  5. `prefix_matches` decides adoption on identity + order, never on field values.

Real Frappe engine + real child rows as the oracle (S.6): the prefix table below is built from actual
`CRM Automation Action` names, never from invented strings.
"""
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import dispatcher, versions
from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist

_RULE_DT = "CRM Automation Rule"
_GRAIN = GRAINS[0]
_PREFIX = "RV-"


def _note(text):
	return {"action_type": "Create Note", "comment_mode": "Literal", "comment_text": text}


def _make_rule(name, action_rows):
	return frappe.get_doc({
		"doctype": _RULE_DT, "rule_name": f"{_PREFIX}{name}", "enabled": 1,
		"on_doctype": "CRM Lead", "event": "Updated",
		"vertical": _GRAIN["vertical"], "group": _GRAIN["group"], "program": _GRAIN["program"],
		"criteria": [], "actions": action_rows,
	}).insert(ignore_permissions=True)


def _cleanup():
	"""By rule-name PREFIX, never by "rules that still exist" — the retire test deletes its rule and
	would otherwise strand its versions.

	Committed, because two paths here (`frappe.delete_doc` in the retire test, and the version purge
	inside `sweep_run_log`) issue DDL that implicitly commits the open transaction — FrappeTestCase's
	rollback can no longer undo anything those tests wrote, so this suite removes it itself."""
	like = ("like", f"{_PREFIX}%")
	frappe.db.delete("CRM Automation Action", {"parent": like})
	frappe.db.delete("CRM Automation Resume", {"rule": like})
	frappe.db.delete(versions.DOCTYPE, {"rule": like})
	frappe.db.delete(_RULE_DT, {"rule_name": like})
	frappe.db.commit()


class TestVersionMinting(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		assert_masters_exist()

	@classmethod
	def tearDownClass(cls):
		_cleanup()

	def test_frozen_action_carries_its_child_row_name(self):
		rule = _make_rule("identity", [_note("a"), _note("b")])
		frozen = versions.load(versions.current_name(rule.name)).actions
		self.assertEqual([a.name for a in frozen], [a.name for a in rule.actions])

	def test_idempotent_save_mints_nothing(self):
		rule = _make_rule("idem", [_note("a")])
		first = versions.current_name(rule.name)
		rule.save(ignore_permissions=True)
		self.assertEqual(versions.current_name(rule.name), first)
		self.assertEqual(frappe.db.count(versions.DOCTYPE, {"rule": rule.name}), 1)

	def test_non_program_fields_do_not_mint(self):
		"""`description`, `enabled` and `priority` are not the program. Editing them must not version it."""
		rule = _make_rule("nonprogram", [_note("a")])
		first = versions.current_name(rule.name)
		rule.description = "reworded"
		rule.enabled = 0
		rule.priority = 7
		rule.save(ignore_permissions=True)
		self.assertEqual(versions.current_name(rule.name), first)

	def test_editing_an_action_mints_a_new_version(self):
		rule = _make_rule("edit", [_note("a")])
		first = versions.current_name(rule.name)
		rule.actions[0].comment_text = "changed"
		rule.save(ignore_permissions=True)
		second = versions.current_name(rule.name)
		self.assertNotEqual(first, second)
		self.assertEqual(frappe.db.get_value(versions.DOCTYPE, second, "version_no"), 2)

	def test_reordering_mints_a_new_version(self):
		"""PLANTED-BAD: were `build_payload` to sort its actions, this hash would not move — and a reorder
		would silently re-route every parked lead."""
		rule = _make_rule("reorder", [_note("a"), _note("b")])
		first = versions.current_name(rule.name)
		rule.actions.insert(0, rule.actions.pop(1))
		rule.save(ignore_permissions=True)
		self.assertNotEqual(versions.current_name(rule.name), first)

	def test_reverting_a_rule_re_flags_its_old_version(self):
		"""Content addressing: going back to a definition already on file reuses that row rather than
		minting a duplicate, and `is_current` follows."""
		rule = _make_rule("revert", [_note("a")])
		v1 = versions.current_name(rule.name)
		rule.actions[0].comment_text = "b"
		rule.save(ignore_permissions=True)
		v2 = versions.current_name(rule.name)
		rule.actions[0].comment_text = "a"
		rule.save(ignore_permissions=True)

		self.assertEqual(versions.current_name(rule.name), v1, "the reverted definition must reuse its version")
		self.assertEqual(frappe.db.count(versions.DOCTYPE, {"rule": rule.name}), 2, "reverting must not mint a third row")
		self.assertEqual(frappe.db.get_value(versions.DOCTYPE, v2, "is_current"), 0, "exactly one version is current")

	def test_a_versions_definition_is_immutable(self):
		rule = _make_rule("immutable", [_note("a")])
		version = frappe.get_doc(versions.DOCTYPE, versions.current_name(rule.name))
		version.payload_json = "{}"
		with self.assertRaises(frappe.exceptions.ValidationError):
			version.save(ignore_permissions=True)

	def test_deleting_a_rule_leaves_no_pinned_version(self):
		"""A deleted rule has no live definition. `is_current` must clear, or the retention sweep — which
		never reclaims a current version — would pin one row per deleted rule, forever."""
		rule = _make_rule("retire", [_note("a")])
		version = versions.current_name(rule.name)
		frappe.delete_doc(_RULE_DT, rule.name, force=1, ignore_permissions=True)
		self.assertEqual(frappe.db.get_value(versions.DOCTYPE, version, "is_current"), 0)


class TestVersionRetention(FrappeTestCase):
	"""`dispatcher.sweep_run_log` reclaims a version only once nothing can ever need it again."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		frappe.db.set_value("CRM Tatva Automation", dispatcher.SWEEP_SWITCH, "enabled", 1)

	@classmethod
	def tearDownClass(cls):
		_cleanup()

	def test_purge_keeps_the_current_version_and_drops_the_superseded_one(self):
		rule = _make_rule("purge", [_note("a")])
		superseded = versions.current_name(rule.name)
		rule.actions[0].comment_text = "b"
		rule.save(ignore_permissions=True)
		current = versions.current_name(rule.name)

		dispatcher.sweep_run_log()

		self.assertTrue(frappe.db.exists(versions.DOCTYPE, current), "a rule's live definition must never be purged")
		self.assertFalse(
			frappe.db.exists(versions.DOCTYPE, superseded),
			"a superseded version no execution and no Run Log references must be reclaimed",
		)

	def test_purge_reclaims_a_version_whose_rule_is_gone(self):
		"""`is_current` protects a live rule, not a ghost. A rule removed by raw SQL never runs `on_trash`,
		so its version stays flagged current — and would otherwise be pinned forever."""
		rule = _make_rule("purge-ghost", [_note("a")])
		orphan = versions.current_name(rule.name)
		frappe.db.delete(_RULE_DT, {"name": rule.name})  # bypasses the lifecycle, exactly like a db-seed

		dispatcher.sweep_run_log()
		self.assertFalse(frappe.db.exists(versions.DOCTYPE, orphan), "a version whose rule is gone must be reclaimable")

	def test_purge_spares_a_superseded_version_an_execution_still_runs(self):
		"""A RETAINED execution keeps its old version alive. Reordering the prefix is what retains it —
		a mere value edit would be adopted, moving the execution forward and leaving the old version
		legitimately reclaimable.

		PLANTED-BAD: drop the Resume reference check in `_purge_unreferenced_versions` and this parked
		lead loses the program it is halfway through."""
		rule = _make_rule("purge-ref", [_note("a"), _note("b")])
		pinned = versions.current_name(rule.name)
		frappe.get_doc({
			"doctype": "CRM Automation Resume", "rule": rule.name, "rule_version": pinned,
			"subject_doctype": "CRM Lead", "subject_name": "irrelevant", "cursor": 1,
			"parked_at": frappe.utils.now_datetime(), "resume_at": frappe.utils.now_datetime(),
			"status": "Pending", "context_json": "{}",
		}).insert(ignore_permissions=True)

		rule.actions.append(rule.actions.pop(0))  # the executed action moves past the cursor -> retained
		rule.save(ignore_permissions=True)
		self.assertEqual(
			frappe.db.get_value("CRM Automation Resume", {"rule": rule.name}, "rule_version"), pinned,
			"the reordered prefix must have retained this execution",
		)

		dispatcher.sweep_run_log()
		self.assertTrue(frappe.db.exists(versions.DOCTYPE, pinned), "a version an execution still runs was purged")
		frappe.db.delete("CRM Automation Resume", {"rule": rule.name})


class TestPrefixMatches(FrappeTestCase):
	"""Adoption is decided on identity + order of the actions the execution has ALREADY run. Field values
	in that prefix are deliberately not compared — the lead ran them; a corrected value cannot reach it."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		cls.rule = _make_rule("prefix", [_note("a"), _note("b"), _note("c"), _note("d")])
		cls.old = versions.load(versions.current_name(cls.rule.name)).actions

	@classmethod
	def tearDownClass(cls):
		_cleanup()

	def _reshaped(self, names):
		by_name = {a.name: a for a in self.old}
		return [dict(by_name[n]) if n in by_name else {"name": n} for n in names]

	def _names(self):
		return [a.name for a in self.old]

	def test_suffix_is_free(self):
		a, b, c, _d = self._names()
		self.assertTrue(versions.prefix_matches(self.old, self._reshaped([a, b, c, "new-row"]), 3))

	def test_prefix_field_values_are_free(self):
		mutated = [dict(a) for a in self.old]
		mutated[1]["comment_text"] = "totally different"
		self.assertTrue(versions.prefix_matches(self.old, mutated, 3))

	def test_reordered_prefix_does_not_match(self):
		a, b, c, d = self._names()
		self.assertFalse(versions.prefix_matches(self.old, self._reshaped([b, a, c, d]), 3))

	def test_deleted_prefix_action_does_not_match(self):
		a, _b, c, d = self._names()
		self.assertFalse(versions.prefix_matches(self.old, self._reshaped([a, c, d]), 3))

	def test_inserted_prefix_action_does_not_match(self):
		a, b, c, d = self._names()
		self.assertFalse(versions.prefix_matches(self.old, self._reshaped([a, "new-row", b, c, d]), 3))

	def test_a_list_shorter_than_the_cursor_does_not_match(self):
		"""The cursor can never point past the end of an ADOPTED version — that is what makes the old
		'Success, 0 actions' silent death unrepresentable."""
		a, b, _c, _d = self._names()
		self.assertFalse(versions.prefix_matches(self.old, self._reshaped([a, b]), 3))

	def test_retyped_prefix_action_does_not_match(self):
		"""AUDIT REGRESSION: Frappe keeps a child row's `name` across an in-place retype, so identity
		alone would let a retyped step (Wait -> Create Note, same row) pass the prefix check — then the
		reschedule would read a non-Wait as the parked Wait. The verb is part of what the execution ran,
		so a changed verb is a changed prefix."""
		by_name = {a.name: a for a in self.old}
		retyped = [dict(by_name[n]) for n in self._names()]
		retyped[1]["action_type"] = "Wait"  # same name, different verb (the fixture rows are Create Note)
		self.assertFalse(versions.prefix_matches(self.old, retyped, 3))


if __name__ == "__main__":
	unittest.main()
