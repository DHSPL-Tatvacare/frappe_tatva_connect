# TEMPORARY — migration reconciliation demo, remove before prod. See REMOVE-ME.md
"""The migration contract, per grain, frozen.

`docs/` is gitignored and absent from the container image, so `mapping.json` cannot be read where
this runs. `grains.json` is generated from it by `_generate_grains.py` — see REMOVE-ME.md.

EVERYTHING VARIES BY GRAIN: which LeadSquared account holds the lead, which event codes were agreed
to move, which fields map where, and even which rows the page may show. Tatvapractice and Inside
Sales migrate no calls at all, so a Calls row must not appear for them — 0 vs 0 would imply calls
were expected. Nothing here may be hardcoded to one account.
"""

import json
import pathlib

_GRAINS_FILE = pathlib.Path(__file__).resolve().parent / "grains.json"


def _load() -> dict:
	return json.loads(_GRAINS_FILE.read_text())


GRAINS: dict[str, dict] = _load()

# The grain masters autoname to a composite key — `CRM Task Type` is {vertical}::{group}::{program}::{type_name}. Authority is taxonomy/labels.py; this only names the separator so no call site spells it inline.
KEY_SEPARATOR = "::"


class UnknownGrain(ValueError):
	pass


class Grain:
	"""One account's migration contract. Every lookup the page needs, answered here."""

	__slots__ = ("_d", "slug")

	def __init__(self, slug: str):
		if slug not in GRAINS:
			raise UnknownGrain(f"Unknown grain: {slug}")
		self.slug = slug
		self._d = GRAINS[slug]

	@property
	def label(self) -> str:
		return self._d["label"]

	@property
	def vertical(self) -> str:
		return self._d.get("vertical") or ""

	@property
	def group(self) -> str:
		return self._d.get("group") or ""

	@property
	def activity_task_types(self) -> dict:
		return self._d["activity_task_types"]

	@property
	def call_log_events(self) -> dict:
		return self._d["call_log_events"]

	@property
	def lsq_event_names(self) -> dict:
		return self._d["lsq_event_names"]

	@property
	def attachment_slots(self) -> dict:
		return self._d["attachment_slots"]

	@property
	def fetches(self) -> list:
		"""Resources read from Frappe for this grain — including ones shown but not judged."""
		return self._d["fetches"]

	@property
	def compares(self) -> list:
		"""The subset of `fetches` that gets a pass/fail verdict."""
		return self._d["compares"]

	@property
	def mapped_event_codes(self) -> frozenset:
		return frozenset(self.activity_task_types) | frozenset(self.call_log_events)

	def field_target(self, lsq_field: str):
		"""LSQ field -> (child table or None, CRM field), or None if not migrated."""
		hit = self._d["field_map"].get(lsq_field)
		return (hit[0], hit[1]) if hit else None

	def event_catalogue(self) -> list[dict]:
		"""Every migrated code: its name in each system, and where it lands."""
		rows = []
		for code in sorted(self.mapped_event_codes, key=int):
			if code in self.call_log_events:
				becomes, lands_in = f"{self.call_log_events[code]} call", "Call log"
			else:
				becomes, lands_in = self.activity_task_types[code], "Activity"
			rows.append(
				{
					"code": code,
					"lsq_name": self.lsq_event_names.get(code, ""),
					"becomes": becomes,
					"lands_in": lands_in,
				}
			)
		return rows


def choices() -> list[dict]:
	"""What the operator picks from."""
	return [
		{"slug": s, "label": g["label"]} for s, g in sorted(GRAINS.items(), key=lambda kv: kv[1]["label"])
	]


def for_lead(vertical: str, group: str) -> str | None:
	"""Reverse lookup: a migrated lead's grain -> the account slug that owns it."""
	for slug, g in GRAINS.items():
		if g.get("vertical") == vertical and g.get("group") == group:
			return slug
	return None


# -- rules that do not vary by grain -----------------------------------------

# Counted on the LSQ side only: only OPEN tasks migrate, as plain to-dos, and those carry no task
# type — so `activity_list` correctly excludes them and there is no Frappe figure to show.
LSQ_ONLY_RESOURCES = {
	"tasks_open": "Only open tasks move across, as to-dos on the lead in Frappe. The partner API "
	"does not list plain to-dos, so no Frappe figure is shown.",
}

# Shown for context, with no Frappe column at all. A LeadSquared task is a reminder; once acted on,
# the action itself is logged as an ACTIVITY carrying the outcome, so a completed task is already
# represented and migrating it too would double-count it.
CONTEXT_ONLY_RESOURCES = {
	"tasks_completed": "A completed task is already recorded as an activity carrying its outcome, "
	"so it is not migrated a second time.",
}

# Read from both systems and shown, but never scored. LeadSquared exposes only a per-activity
# `HasAttachments` flag, not a file count, so an honest total would need one extra call per
# activity — 122 of them on a busy lead. The figures come from different units on each side
# (LeadSquared note attachments vs Frappe File rows) and are shown for context only. Marking this
# "Different" would raise an alarm the tool cannot substantiate.
INDICATIVE_RESOURCES = {
	"files": "Counted differently by each system, so the two figures are shown for reference and "
	"are not compared. LeadSquared reports only whether an activity carries attachments, not how "
	"many.",
}

# Differences the pipeline caused ON PURPOSE. Shown as an attribution on the affected row — the row
# still reports its two figures and still says they differ, and the note says who did it and why.
#
# THE COLLAPSE FIRES ON EVERY EVENT CODE, not only on calls. LeadSquared writes the same record twice
# under two different ids, and the migration keeps one of each identical pair (harness/rules.py); an
# ACTIVITY written twice is collapsed exactly as a call is. Footnoting `calls` alone therefore left the
# same deliberate act reading as an unexplained variance on the activities row — which is what put
# "3 differences to review" on a report whose pipeline had done all three on purpose.
#
# The direction is stated because it is the reader's only check on the claim: a collapse can only ever
# make the Frappe figure LOWER. More rows in Frappe than in LeadSquared is not this, and is a real gap.
_COLLAPSED_DUPLICATES = (
	"LeadSquared records some of these twice, under two different ids. The migration keeps one row per "
	"identical pair, so the Frappe figure is lower by exactly what was collapsed — never higher."
)

EXPECTED_DIFFERENCES = {
	"activities": _COLLAPSED_DUPLICATES,
	"calls": _COLLAPSED_DUPLICATES,
}

# The loader's own rule for "this task is done" — kept identical to sink_orm.load_tasks.
COMPLETED_TASK_STATUSES = ("1", "Completed")

RESOURCE_LABELS = {
	"activities": "Activities",
	"notes": "Notes",
	"calls": "Calls",
	"files": "Files",
	"tasks_open": "Open tasks",
	"tasks_completed": "Completed tasks",
}
