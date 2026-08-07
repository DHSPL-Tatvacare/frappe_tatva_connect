# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""OFFLINE validator for the TatvaPractice Phase-2 seeds — what they touch, and that they ship.

Three things this locks, none of which a bench can tell you before the operator has already run the file:

1. THE LOCATION SEED TOUCHES THE SIX AND ONLY THE SIX. `Contact Type` is out by measurement, not by
   taste: LSQ's own `TrackLocation` is 0 on event 210 and 1 on all fourteen other TP events, phone-call
   variants included. A seventh type creeping into that list is a silent change to what the migration
   demands of a rep, so the set is asserted exactly. `Manager Visit` is already In-Person from tp-04 and
   is deliberately not re-written here.

2. THE TWO INFERRED CONDITIONS ARE CLEARED AND NO THIRD IS BORN. tp-06 conditioned capture on
   `meeting_type is 'Physical Visit'` and `training_type is 'Physical'`; both were read off the BRD
   before the LSQ setting was, and both under-capture. Every `location_condition_*` / `location_operator`
   assignment in the seed must therefore be NULL — a re-narrowing would otherwise pass unnoticed.

3. THE SEED IS ACTUALLY REGISTERED, in `seeds.manifest` AND in `INDEX.md`. `2026-08-01-form-layout-breaks.sql`
   was written, shipped to the VM, listed in no manifest, and never ran while the docs recorded its work as
   done. An unregistered seed is a seed that does not exist.

Plus one on the layout seed: `Doctor Training Field Visit` is being STARVED by Phase 1, so a design aimed
at it is a design for a form no rep will open. It is not repointed at `Doctor Training` — the two declare
different fieldnames (`training_type` / `training_completed_date_time` vs `lsq_status`;
`was_the_asm_along_with_you` vs `was_asm_along_with_you`) and the layout preflight would abort the run.

And one on tp-06 itself: THE SIX NAMES ARE NOT RETYPED WITHOUT A CROSS-CHECK. Both seeds key their
`UPDATE`s on `name`, and a `name` that no longer exists matches zero rows and exits 0 — so a RENAME in
`2026-07-26-tp-06-activity-rules.sql` alone leaves this file, the location seed and every assertion in
lockstep and green while nothing at all is written. `seeds.manifest:52` records that class already
happening once. So the set the location seed touches is asserted to sit INSIDE the set tp-06 declares,
read out of tp-06 with the same scanner rather than typed again.

T-TP-4 is why `_strip_sql_comments` exists: a `--` comment in these files contains apostrophes and
semicolons, and a naive scan reads them as SQL. Comments are stripped quote-aware BEFORE anything is read.

The bundle these read is gitignored by design (`.gitignore:36`), so a checkout without it SKIPS rather
than erroring — the same call `test_derived_head.py:52` already made, for the same reason: an absent
gitignored input is not a fault in the thing under test.

Pure stdlib — no frappe, no bench, no database, no network.

Run:
    python3 -m unittest tatva_connect.tests.static.test_tp_seed_fidelity
"""
import ast
import pathlib
import re
import unittest

from tatva_connect.tests.static._lock_helpers import app_root

SEEDS = pathlib.Path(app_root(__file__)).parent / "docs" / "go-live" / "3-seed" / "db-seeds"

LOCATION_SEED = "2026-08-05-tp-location-capture.sql"
LAYOUT_SEED = "2026-07-27-activity-form-layout.bench-console.py"
RULES_SEED = "2026-07-26-tp-06-activity-rules.sql"
MANIFEST = "seeds.manifest"
INDEX = "INDEX.md"

GRAIN = "Tatvapractice::India::Field-Sales"

# The BRD's six INTENT (DEC-4; Contact Type is out on TrackLocation 0) — tp-06 owns the SPELLING.
THE_SIX = {
	"Introductory Meeting",
	"Demo Scheduled Status",
	"Onboarding Status",
	"Doctor Training",
	"Doctor Activation",
	"Courtesy Visit",
}

# The columns that decide WHEN a location is demanded. All three must be cleared, none re-declared.
_LOCATION_COLUMNS = ("location_condition_field", "location_operator", "location_condition_value")

_COMPOSITE = re.compile(r"'(" + re.escape(GRAIN) + r"::[^']+)'")
_ASSIGN = re.compile(r"`(\w+)`\s*=\s*('[^']*'|NULL)", re.IGNORECASE)


def _bundle(name):
	"""One file of the go-live bundle. Gitignored by design, so a checkout without it skips rather than
	lies — the same call `test_derived_head.py:52` makes."""
	path = SEEDS / name
	if not path.exists():
		raise unittest.SkipTest(f"the go-live bundle is not in this checkout: {path}")
	return path.read_text(encoding="utf-8")


def _strip_sql_comments(sql):
	"""The SQL with every `--` line comment removed, quote-aware (T-TP-4)."""
	out, i, in_quote, in_tick = [], 0, False, False
	while i < len(sql):
		ch = sql[i]
		if in_quote and ch == "\\":
			out.append(sql[i : i + 2])
			i += 2
			continue
		if ch == "'" and not in_tick:
			in_quote = not in_quote
		elif ch == "`" and not in_quote:
			in_tick = not in_tick
		elif ch == "-" and sql[i : i + 2] == "--" and not in_quote and not in_tick:
			i = sql.find("\n", i)
			if i == -1:
				break
			continue
		out.append(ch)
		i += 1
	return "".join(out)


def _statements(sql):
	"""The seed's executable statements, comments gone, split on the statement terminator."""
	return [s.strip() for s in _strip_sql_comments(sql).split(";") if s.strip()]


def _task_types(text):
	"""Every TP task type a chunk of SQL addresses — the 4th segment, so a `::fld::` row resolves to
	its parent type rather than to a field name."""
	return {m.group(1).split("::")[3] for m in _COMPOSITE.finditer(text)}


def _assignments(text, column):
	"""Every value assigned to `column` in this chunk of SQL, as written."""
	return [m.group(2) for m in _ASSIGN.finditer(text) if m.group(1).lower() == column]


def _declared_types(seed):
	"""Every TP task type a seed file names, through the one scanner — so nothing is retyped here."""
	return _task_types(_strip_sql_comments(_bundle(seed)))


def _manifest_entries():
	"""The seed filenames `apply-seeds.sh` will actually run — commented lines are not run."""
	lines = _bundle(MANIFEST).splitlines()
	return [ln.split("#")[0].strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


def _designed_types():
	"""The task types the layout seed hand-designs — its `DESIGNED` literal, read as data."""
	tree = ast.parse(_bundle(LAYOUT_SEED))
	for node in ast.walk(tree):
		if isinstance(node, ast.Assign) and any(
			isinstance(t, ast.Name) and t.id == "DESIGNED" for t in node.targets
		):
			return set(ast.literal_eval(node.value))
	raise AssertionError(f"no DESIGNED map in {LAYOUT_SEED} — this lock is watching nothing")


class TestTPLocationSeed(unittest.TestCase):
	def setUp(self):
		self.sql = _bundle(LOCATION_SEED)

	def test_it_touches_the_six_and_only_the_six(self):
		self.assertEqual(_task_types(_strip_sql_comments(self.sql)), THE_SIX)

	def test_every_type_it_touches_is_one_tp_06_really_declares(self):
		"""Both seeds key on `name`, so a rename in tp-06 alone leaves this UPDATE matching zero rows
		and exiting 0 — green everywhere, written nowhere (`seeds.manifest:52`)."""
		self.assertLessEqual(_task_types(_strip_sql_comments(self.sql)), _declared_types(RULES_SEED))

	def test_contact_type_is_never_named(self):
		"""LSQ set TrackLocation = 0 on event 210 deliberately; declaring it here would overrule that."""
		self.assertNotIn("Contact Type", _task_types(_strip_sql_comments(self.sql)))

	def test_the_only_visit_mode_it_writes_is_in_person(self):
		modes = _assignments(_strip_sql_comments(self.sql), "visit_mode")
		self.assertTrue(modes, "the seed declares no visit_mode at all")
		self.assertEqual(set(modes), {"'In-Person'"})

	def test_both_inferred_conditions_are_cleared(self):
		"""Introductory Meeting's `meeting_type` and Doctor Training's `training_type` legs, both gone."""
		cleared = set()
		for stmt in _statements(self.sql):
			if any(_assignments(stmt, col) for col in _LOCATION_COLUMNS):
				cleared |= _task_types(stmt)
		self.assertEqual(cleared, {"Introductory Meeting", "Doctor Training"})

	def test_it_introduces_no_new_location_condition(self):
		body = _strip_sql_comments(self.sql)
		for col in _LOCATION_COLUMNS:
			written = _assignments(body, col)
			self.assertTrue(written, f"{col} is never written, so nothing is cleared")
			self.assertEqual(
				set(written), {"NULL"},
				f"{col} is re-declared as {written} — a condition narrows what In-Person already covers",
			)

	def test_it_is_registered_in_the_manifest(self):
		self.assertIn(LOCATION_SEED, _manifest_entries())

	def test_it_is_registered_in_the_index(self):
		self.assertIn(LOCATION_SEED, _bundle(INDEX))

	def test_it_runs_after_the_seed_that_wrote_the_conditions(self):
		entries = _manifest_entries()
		self.assertLess(entries.index(RULES_SEED), entries.index(LOCATION_SEED))


class TestLayoutSeedStarvesTheRetiredType(unittest.TestCase):
	def test_doctor_training_field_visit_carries_no_design(self):
		designed = _designed_types()
		self.assertTrue(designed, "the DESIGNED map is empty")
		self.assertNotIn(f"{GRAIN}::Doctor Training Field Visit", designed)

	def test_the_design_is_not_repointed_at_the_collapsed_type(self):
		"""The two declare different fieldnames, so a repoint would abort the layout preflight."""
		self.assertNotIn(f"{GRAIN}::Doctor Training", _designed_types())


class TestTheScannerItself(unittest.TestCase):
	def test_an_apostrophe_in_a_comment_does_not_open_a_string(self):
		sql = "-- LSQ's list of values\nUPDATE `t` SET `visit_mode`='In-Person';"
		self.assertEqual(_strip_sql_comments(sql).strip(), "UPDATE `t` SET `visit_mode`='In-Person';")

	def test_a_semicolon_in_a_comment_does_not_end_a_statement(self):
		sql = "-- Show only; nothing else.\nUPDATE `t` SET `a`=NULL;"
		self.assertEqual(_statements(sql), ["UPDATE `t` SET `a`=NULL"])

	def test_a_double_dash_inside_a_quoted_value_is_not_a_comment(self):
		sql = "UPDATE `t` SET `label`='A -- B', `visit_mode`='In-Person';"
		self.assertEqual(_assignments(_strip_sql_comments(sql), "visit_mode"), ["'In-Person'"])

	def test_a_field_row_name_resolves_to_its_parent_type(self):
		self.assertEqual(_task_types(f"'{GRAIN}::Doctor Training::fld::training_type'"), {"Doctor Training"})
