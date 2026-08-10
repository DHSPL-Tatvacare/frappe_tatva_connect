# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Activity RULES are seeded AFTER the LeadSquared load, never before it. The phase order, locked offline.

`compute_activity` REFUSES a value for a field the form's rules HIDE — rule D22, `activity/api.py:745-750`.
Simulated over the real 105,851-row TatvaPractice corpus against the real tp-06 rule set, 1,426 rows carry
a value those rules hide and would be refused: `select_asm` 837 (365 event-207 rows answer "Was the ASM
along with you? = No" and still carry an ASM id, because LSQ's old form showed every field at once),
`visit_follow_up_date_time` 154, `visit_completed_next_steps` 129. The SAME simulation with the rules not
yet seeded refuses NOTHING — a field no rule names keeps its own null condition, so nothing is hidden and
nothing is rejected.

So history loads exactly as LeadSquared recorded it, and every activity logged afterwards follows the
rules. That is an ORDER, and an order written only in a file header is an order nobody keeps. The two
manifests ARE the declaration: `seeds.manifest` is everything the loader needs before it writes a single
activity — task types, their fields, options and picklists — and `seeds-post-load.manifest` is the rules
plus the form layout, which reads them.

The layout seed is in the second list for its own reason: it asks `_has_rules(task_type)` to choose one
column for a cascade against two for a form that hides nothing, so run before the rules exist it lays out
every cascade as two columns.

Pure stdlib — no frappe, no bench, no database, no network.

Run:
    python3 -m unittest tatva_connect.tests.static.test_seed_phase_order
"""
import pathlib
import re
import unittest

from tatva_connect.tests.static._lock_helpers import app_root

# The quote-aware `--` stripper T-TP-4 exists for; one scanner, not a second copy of it.
from tatva_connect.tests.static.test_tp_seed_fidelity import _strip_sql_comments

SEEDS = pathlib.Path(app_root(__file__)).parent / "docs" / "go-live" / "3-seed" / "db-seeds"

# The manifests, by their CURRENT names — renamed from seeds.manifest / seeds-post-load.manifest, which
# this file went on reading until 2026-08-10: every test errored on a missing file, so the phase order
# was unguarded for the whole of that window. setUp below turns that failure mode into a named one.
PRE_LOAD = "1-before-load.manifest"
POST_LOAD = "2-after-load.manifest"
LAYOUT_SEED = "2026-07-27-activity-form-layout.bench-console.py"

# Any statement, not just INSERT: a pre-load UPDATE amends rows that do not exist yet, matches none, and exits 0.
_RULE_WRITE = re.compile(
	r"(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+`?tabCRM Task Type Rule`?", re.IGNORECASE)


def _entries(manifest):
	"""The seed filenames `apply-seeds.sh` will actually run — a commented line is not run."""
	lines = (SEEDS / manifest).read_text(encoding="utf-8").splitlines()
	return [ln.split("#")[0].strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


def _body(entry):
	"""A seed's executable text: SQL comments stripped, so a header line NAMING its post-load twin
	never reads as a rule write."""
	text = (SEEDS / entry).read_text(encoding="utf-8")
	return _strip_sql_comments(text) if entry.endswith(".sql") else text


class _BundleCase(unittest.TestCase):
	"""`docs/` is gitignored by design, so a checkout without the go-live bundle SKIPS rather than lies."""

	def setUp(self):
		if not SEEDS.exists():
			self.skipTest(f"the go-live bundle is not in this checkout: {SEEDS}")
		# The bundle IS here, so a manifest that is not is a rename, and this guard must say so rather than error.
		for manifest in (PRE_LOAD, POST_LOAD):
			self.assertTrue((SEEDS / manifest).exists(), f"{manifest} is gone — renamed? this guard reads it by name")


class TestNoRuleReachesTheLoad(_BundleCase):
	def test_no_pre_load_seed_writes_a_task_type_rule(self):
		offenders = [e for e in _entries(PRE_LOAD) if _RULE_WRITE.search(_body(e))]
		self.assertEqual(
			offenders, [],
			"these run BEFORE the LSQ load, so every field their rules hide makes compute_activity "
			"refuse the value LeadSquared already recorded (D22). Rules belong in " + POST_LOAD,
		)


class TestThePostLoadManifest(_BundleCase):
	def test_every_seed_it_names_is_on_disk(self):
		missing = [e for e in _entries(POST_LOAD) if not (SEEDS / e).exists()]
		self.assertEqual(missing, [], "named in the manifest, absent from the folder")

	def test_the_two_manifests_share_no_seed(self):
		both = sorted(set(_entries(PRE_LOAD)) & set(_entries(POST_LOAD)))
		self.assertEqual(both, [], "a seed in both lists runs twice and its phase means nothing")

	def test_the_layout_seed_runs_after_the_rules(self):
		# By BASENAME: an entry now carries its phase folder, and comparing the whole string would pass
		# vacuously the day a folder is renamed — the same silence the manifest rename already bought.
		self.assertIn(LAYOUT_SEED, [pathlib.PurePath(e).name for e in _entries(POST_LOAD)])
		self.assertNotIn(LAYOUT_SEED, [pathlib.PurePath(e).name for e in _entries(PRE_LOAD)])
