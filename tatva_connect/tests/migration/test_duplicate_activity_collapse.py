"""The migration's duplicate collapse must remove REDUNDANCY, never information.

`harness/rules.py` keeps one row per (lead, event, second) because LeadSquared's telephony webhook
writes the same call twice under two ids. Keyed on those three things alone, the rule was GUESSING at
sameness: measured over the 287,249-row Anaya extract it collapsed 9,374 rows, of which 9,369 were
byte-identical and 5 were not. The worst was a Document Upload where a rep's row — a real author,
"Documents uploaded" — lost to an empty System row purely because its id sorted lower.

So the window narrows the candidates and the CONTENT decides. Both fixtures below are real shapes out
of that extract, with ids, names and addresses replaced by placeholders; the id ORDER is preserved,
because it is the id order that made the old rule drop the row a human wrote.

The rule is pure Python and lives in the migration toolkit, which is gitignored — so this skips where
the toolkit is absent rather than failing a checkout that never had it.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.migration.test_duplicate_activity_collapse
"""
import importlib.util
import pathlib
import unittest

_RULES = pathlib.Path(__file__).resolve().parents[3] / "docs" / "go-live" / "7-migrate-data" / "harness" / "rules.py"


def _load_rules():
	spec = importlib.util.spec_from_file_location("harness_rules", _RULES)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


# LeadSquared wrote this Document Upload twice: two ids, two ModifiedOn stamps, one real event.
IDENTICAL_PAIR = [
	{
		"ProspectActivityId": "5c95aedc-0000-0000-0000-000000000001",
		"RelatedProspectId": "7a8139b7-0000-0000-0000-00000000000a",
		"ActivityType": "2", "ActivityEvent": "231",
		"CreatedOn": "2026-01-13 07:17:16",
		"CreatedBy": "c40bfe52-0000-0000-0000-0000000000ff",
		"CreatedByEmailAddress": "system@example.invalid", "CreatedByName": "System ",
		"ModifiedOn": "2026-01-13 07:17:16",
		"ModifiedBy": "c40bfe52-0000-0000-0000-0000000000ff",
		"Extension_ModifiedBy": "c40bfe52-0000-0000-0000-0000000000ff",
		"ModifiedByEmailAddress": "system@example.invalid", "ModifiedByName": "System ",
		"Status": "Active", "mx_Custom_2": "Yes", "mx_Custom_3": "Yes", "_lsq_event_code": "231",
	},
	{
		"ProspectActivityId": "2486782e-0000-0000-0000-000000000002",
		"RelatedProspectId": "7a8139b7-0000-0000-0000-00000000000a",
		"ActivityType": "2", "ActivityEvent": "231",
		"CreatedOn": "2026-01-13 07:17:16",
		"CreatedBy": "c40bfe52-0000-0000-0000-0000000000ff",
		"CreatedByEmailAddress": "system@example.invalid", "CreatedByName": "System ",
		"ModifiedOn": "2026-01-13 07:17:17",
		"ModifiedBy": "c40bfe52-0000-0000-0000-0000000000ff",
		"Extension_ModifiedBy": "c40bfe52-0000-0000-0000-0000000000ff",
		"ModifiedByEmailAddress": "system@example.invalid", "ModifiedByName": "System ",
		"Status": "Active", "mx_Custom_2": "Yes", "mx_Custom_3": "Yes", "_lsq_event_code": "231",
	},
]

# Same lead, same event, same second — and NOT the same record. A rep logged an upload; LeadSquared's
# own automation logged an empty row alongside it. The System id sorts lower, so "lowest id wins" chose
# the empty one and the rep's row, with its author and its outcome, was dropped.
DIFFERING_PAIR = [
	{
		"ProspectActivityId": "ffffffff-0000-0000-0000-000000000001",
		"RelatedProspectId": "b10a10ba-0000-0000-0000-00000000000b",
		"ActivityType": "2", "ActivityEvent": "231",
		"CreatedOn": "2025-01-03 06:12:46",
		"CreatedBy": "cff2cf1b-0000-0000-0000-0000000000ee",
		"CreatedByEmailAddress": "rep@example.invalid", "CreatedByName": "A Rep",
		"ModifiedOn": "2025-01-03 06:12:46",
		"ModifiedBy": "cff2cf1b-0000-0000-0000-0000000000ee",
		"Extension_ModifiedBy": "cff2cf1b-0000-0000-0000-0000000000ee",
		"ModifiedByEmailAddress": "rep@example.invalid", "ModifiedByName": "A Rep",
		"Owner": "cff2cf1b-0000-0000-0000-0000000000ee",
		"mx_Custom_1": "Yes", "mx_Custom_2": "Yes", "mx_Custom_3": "Yes",
		"mx_Custom_7": "Documents uploaded", "_lsq_event_code": "231",
	},
	{
		"ProspectActivityId": "11111111-0000-0000-0000-000000000002",
		"RelatedProspectId": "b10a10ba-0000-0000-0000-00000000000b",
		"ActivityType": "2", "ActivityEvent": "231",
		"CreatedOn": "2025-01-03 06:12:46",
		"CreatedBy": "c40bfe52-0000-0000-0000-0000000000ff",
		"CreatedByEmailAddress": "system@example.invalid", "CreatedByName": "System ",
		"ModifiedOn": "2025-01-03 06:12:46",
		"ModifiedBy": "c40bfe52-0000-0000-0000-0000000000ff",
		"Extension_ModifiedBy": "c40bfe52-0000-0000-0000-0000000000ff",
		"ModifiedByEmailAddress": "system@example.invalid", "ModifiedByName": "System ",
		"Status": "Active", "mx_Custom_2": "Yes", "mx_Custom_3": "Yes", "_lsq_event_code": "231",
	},
]


@unittest.skipUnless(_RULES.exists(), "migration toolkit not present in this checkout")
class TestDuplicateActivityCollapse(unittest.TestCase):
	def setUp(self):
		self.rules = _load_rules()

	def test_an_identical_pair_collapses_to_its_lowest_id(self):
		"""Two writes of one event: one row survives, and which one is decided the same way every run."""
		dropped = {}
		kept = self.rules.collapse_duplicate_activities(IDENTICAL_PAIR, dropped)
		self.assertEqual(len(kept), 1, "LeadSquared's double write must not become two records")
		self.assertEqual(kept[0]["ProspectActivityId"], "2486782e-0000-0000-0000-000000000002",
						 "the survivor is the lowest activity id, so a re-run collapses the same way")
		self.assertEqual(dropped, {"231": 1}, "what was collapsed must be reportable, per event code")

	def test_rows_that_differ_all_survive(self):
		"""The window is not evidence of sameness. Two rows saying different things are two records —
		and the one the old rule dropped is the one a human wrote."""
		dropped = {}
		kept = self.rules.collapse_duplicate_activities(DIFFERING_PAIR, dropped)
		self.assertEqual(len(kept), 2, "a differing row must never be collapsed away")
		self.assertEqual(dropped, {}, "nothing was collapsed, so nothing may be reported as collapsed")
		self.assertIn("Documents uploaded", [row.get("mx_Custom_7") for row in kept],
					  "the row carrying the outcome a rep recorded must survive")

	def test_the_window_still_bounds_the_comparison(self):
		"""Content decides WITHIN the window, it does not replace it: two identical-looking rows on
		different leads are two records, and an unkeyable row is never collapsed at all."""
		other_lead = dict(IDENTICAL_PAIR[0], RelatedProspectId="0000ffff-0000-0000-0000-00000000000c")
		self.assertEqual(len(self.rules.collapse_duplicate_activities([IDENTICAL_PAIR[0], other_lead])), 2)

		undated = [dict(row, CreatedOn="") for row in IDENTICAL_PAIR]
		self.assertEqual(len(self.rules.collapse_duplicate_activities(undated)), 2,
						 "no timestamp means no window, and what cannot be keyed is never collapsed")
