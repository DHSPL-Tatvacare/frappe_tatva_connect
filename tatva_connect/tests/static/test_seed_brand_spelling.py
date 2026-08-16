# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""No seed writes a business-line name in a case the app has retired.

WHY THIS EXISTS. `Goodflip` is the owner-authorised spelling — it is the vertical, the group and the
programme, and `patches/normalise_grain_spelling` folds every other case onto it. One seed nonetheless
renamed the `Goodflip` LEAD SOURCE to `GoodFlip`, the loader normalised the data to match, and three
Anaya workflow predicates were written against that spelling. All three agreed with each other and
disagreed with the rest of the app.

That is not cosmetic, and this is the reason the check is worth its lines: the DATABASE compares
case-insensitively (`utf8mb4_unicode_ci`), so every lookup kept working — but a workflow predicate is
evaluated in PYTHON (`automation/rules._in_list` -> `_eq_typed` -> `==`), which is case-SENSITIVE.
Measured: a lead stored as `Goodflip` does NOT match a rule written `GoodFlip`. The branch takes the
wrong route, silently, on a live patient.

WHAT IT ALLOWS. Only a STANDALONE word is judged. `GoodFlip Lite 3 Months - Diabetes Care` and
`GoodFlip GLP-1 Landing Page` are LeadSquared's own product and source names, seeded verbatim under the
honour-LeadSquared rule — a different string, not this bug, and left alone.

Pure stdlib — no frappe, no bench, no database, no network.

Run:
    python3 -m unittest tatva_connect.tests.static.test_seed_brand_spelling
"""
import pathlib
import re
import unittest

from tatva_connect.tests.static._lock_helpers import app_root

_APP = pathlib.Path(app_root(__file__))
SEEDS = _APP.parent / "docs" / "go-live" / "3-seed" / "db-seeds"
MAPPINGS = _APP.parent / "docs" / "go-live" / "7-migrate-data"

# The authorised spelling of every multi-case business-line word, read from the patch that owns the
# vocabulary rather than retyped here — a second copy is what drifts.
def _canon() -> set:
	src = (_APP / "patches" / "normalise_grain_spelling.py").read_text(encoding="utf-8")
	block = src.split("CANON = (")[1].split(")")[0]
	return {w for w in re.findall(r'"([^"]+)"', block) if w.lower() != w and w.upper() != w}


# Judged and allowed, with the reason. A word here is NOT a grain axis, a master name or a predicate
# operand — the three places a casing actually decides behaviour. Each entry is a reading of the file.
ALLOWED = {
	# A filesystem folder, lowercase by convention: docs/go-live/7-migrate-data/anaya/data/leads.jsonl
	("2026-08-12-anaya-provider-masters.bench-console.py", "anaya"),
	# Insights DISPLAY labels — workbook title, team name, query names. Its grain scope is a separate
	# constant and correctly reads `Tatvapractice`, which is what the data is filtered on.
	("2026-08-15-insights-02-tatvapractice-workbook.bench-console.py", "TatvaPractice"),
	# The fold-FROM value in the correction itself: `WHERE name = 'GoodFlip'` is how the retired casing
	# is found and removed. Naming it is the point of the statement.
	("2026-08-09-anaya-00-prerequisite-fields.sql", "GoodFlip"),
}


def _quoted_values(text: str) -> set:
	"""Every discrete VALUE a seed writes, split out of its quoted literals.

	Only a literal is judged, never prose — a comment reading "the TatvaPractice reps" decides nothing,
	while `'Tatvapractice::India::Field-Sales'` decides everything. Each literal is then split on commas,
	because `"Inbound Phone call,GoodFlip"` is the shape a workflow predicate takes and the value that
	matters is the list ITEM. An earlier version of this check judged whole literals only and so missed
	exactly the defect it was written for.
	"""
	out = set()
	for literal in re.findall(r"'([^'\n]*)'|\"([^\"\n]*)\"", text):
		for raw in (literal[0] or literal[1] or "").split(","):
			item = raw.strip()
			if item:
				out.add(item)
	return out


class TestSeedBrandSpelling(unittest.TestCase):
	def setUp(self):
		if not SEEDS.is_dir():
			raise unittest.SkipTest(f"the go-live bundle is not in this checkout: {SEEDS}")
		self.canon = _canon()
		self.assertTrue(self.canon, "no CANON vocabulary found — this lock would be watching nothing")

	def _scan(self, paths):
		bad = {}
		for path in paths:
			text = path.read_text(encoding="utf-8", errors="ignore")
			hits = [
				(v, g) for v in _quoted_values(text) for g in self.canon
				if v != g and v.lower() == g.lower() and (path.name, v) not in ALLOWED
			]
			if hits:
				bad[path.name] = hits
		return bad

	def test_no_seed_writes_a_retired_casing(self):
		paths = [p for d in ("1-before-load", "2-after-load") for p in (SEEDS / d).glob("*")
		         if p.suffix in {".sql", ".py"}]
		bad = self._scan(paths)
		self.assertEqual(
			bad, {},
			"a seed writes a business-line name in a case the app retired. The database compares "
			"case-insensitively so lookups still work, but a workflow predicate compares in PYTHON and "
			"does not — the rule silently stops matching. Fold onto the authorised spelling:\n"
			+ "\n".join(f"  {f}: {h}" for f, h in bad.items()),
		)

	def test_no_loader_mapping_normalises_towards_a_retired_casing(self):
		"""The mapping decides what the DATA is stored as, so a wrong TARGET there defeats every seed.

		Only the target is judged. Naming the retired casing on the LEFT is how a fold finds what to
		correct — `{"GoodFlip": "Goodflip"}` is the fix, not the fault."""
		if not MAPPINGS.is_dir():
			raise unittest.SkipTest("the migration bundle is not in this checkout")
		import json

		bad = {}
		for path in sorted(MAPPINGS.glob("*/mapping.json")):
			targets = set()
			for field_map in (json.loads(path.read_text()).get("value_normalisation") or {}).values():
				if isinstance(field_map, dict):
					targets |= {str(v) for v in field_map.values()}
			wrong = [(t, g) for t in targets for g in self.canon if t != g and t.lower() == g.lower()]
			if wrong:
				bad[path.parent.name] = sorted(set(wrong))
		self.assertEqual(
			bad, {},
			"a loader normalises the DATA onto a casing the app retired, so every lead lands on the wrong "
			f"spelling however the masters are seeded: {bad}",
		)


if __name__ == "__main__":
	unittest.main()
