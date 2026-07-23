# TEMPORARY — migration reconciliation demo, remove before prod. See REMOVE-ME.md
"""Generates `grains.json` from the migration toolkit's `mapping.json` files.

RUN BY HAND, NEVER AT RUNTIME. `docs/` is gitignored and absent from the container image, so the
page cannot read `mapping.json` where it runs — the contract has to be baked in and shipped.

    cd <repo root>
    python3 tatva_connect/migration_check/_generate_grains.py

Re-run whenever a mapping changes. If that happens more than once, this tool has outlived its
purpose and should be deleted rather than maintained.
"""

import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
TOOLKIT = REPO / "docs" / "go-live" / "7-migrate-data"
OUT = pathlib.Path(__file__).resolve().parent / "grains.json"

# account slug -> the label an operator picks from. Vertical – group, never program: one account
# serves many programs (Anaya alone covers Nivolumab, Sigrima, Tukavo, Ujvira).
ACCOUNTS = {
	"anaya": "Goodflip-Care \u2013 Anaya",
	"tatvapractice": "Tatvapractice \u2013 India",
	"goodflip_inside_sales": "Goodflip \u2013 India (Inside Sales)",
}


def lsq_event_names(account: str) -> dict:
	"""LSQ's own display name per event code, from the scope snapshot."""
	path = TOOLKIT / account / "scope" / "activity_types.json"
	if not path.exists():
		return {}
	names = {}
	for row in json.loads(path.read_text()):
		code = row.get("ActivityEventCode") or row.get("ActivityEvent") or row.get("EventCode")
		label = row.get("ActivityEventName") or row.get("ActivityName")
		if code is not None and label:
			names[str(code)] = label
	return names


def build(account: str, label: str) -> dict | None:
	path = TOOLKIT / account / "mapping.json"
	if not path.exists():
		print(f"  skip {account}: no mapping.json", file=sys.stderr)
		return None

	m = json.loads(path.read_text())
	routing = m.get("routing") or {}
	task_types = {str(k): v for k, v in (m.get("activity_task_types") or {}).items()}
	call_events = {str(k): v for k, v in (m.get("call_log_events") or {}).items()}
	names = lsq_event_names(account)

	field_map = {}
	for lsq, crm in (m.get("lead_core") or {}).items():
		field_map[lsq] = [None, crm]
	for table, spec in (m.get("children") or {}).items():
		for lsq, crm in (spec.get("fields") or {}).items():
			field_map[lsq] = [table, crm]

	# What to read from Frappe, and which of those get a pass/fail verdict.
	#
	# A grain with no call events must not display a Calls row — 0 vs 0 would imply calls were
	# expected here. Files are FETCHED but never judged: LeadSquared exposes only a per-activity
	# `HasAttachments` flag, not a count, so an honest total needs one extra call per activity.
	# Both figures are shown; neither is scored. See INDICATIVE_RESOURCES.
	fetches = ["activities", "notes", "files"]
	compares = ["activities", "notes"]
	if call_events:
		fetches.insert(2, "calls")
		compares.append("calls")

	return {
		"label": label,
		"vertical": routing.get("custom_vertical"),
		"group": routing.get("custom_group"),
		"activity_task_types": task_types,
		"call_log_events": call_events,
		"lsq_event_names": {c: names.get(c, "") for c in {**task_types, **call_events}},
		"attachment_slots": {str(k): list(v) for k, v in (m.get("activity_attachments") or {}).items()},
		"field_map": field_map,
		"fetches": fetches,
		"compares": compares,
	}


def main() -> int:
	if not TOOLKIT.exists():
		print(f"Migration toolkit not found at {TOOLKIT}", file=sys.stderr)
		return 1

	grains = {}
	for account, label in ACCOUNTS.items():
		built = build(account, label)
		if built:
			grains[account] = built
			print(
				f"  {account:24} {len(built['activity_task_types']):>3} activity types, "
				f"{len(built['call_log_events'])} call events, {len(built['field_map']):>3} fields"
			)

	OUT.write_text(json.dumps(grains, indent=1, ensure_ascii=False) + "\n")
	print(f"wrote {OUT.relative_to(REPO)} ({len(grains)} grains)")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
