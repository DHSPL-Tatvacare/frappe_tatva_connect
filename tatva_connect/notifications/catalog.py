"""The ONE notification catalog — every notifiable moment is one row here.

A grain is a registered moment a rep can be told about (a lead assigned, a task
assigned, …). Each grain is gated twice: the operator's GLOBAL switch
(`is_enabled(automation_key)` — the ONE gate, owned by the automation plane) and the
rep's per-user OPT-IN (default OFF). Dispatch, the prefs panel, and opt-in storage all
DERIVE from this catalog — nothing is special-cased per grain in code.

Mirrors `automation/registry.py`: a frozen dataclass + a flat list + a `get()` accessor.
`automation_key` MUST name an `Auto` row in registry.py — the drift guard (drift.py)
fails `bench migrate` if it doesn't.
"""
from dataclasses import dataclass

CHANNELS = ("live",)  # known channels; "email" lands in Phase 4. Drift asserts membership.


@dataclass(frozen=True)
class NotifGrain:
	key: str             # "Lead::Assignment::assigned"  (Area::Source::event)
	label: str           # shown in the prefs panel
	description: str      # one line, user-facing
	automation_key: str   # the CRM Tatva Automation row that globally enables it (the ONE gate)
	channels: tuple       # subset of CHANNELS
	default_optin: bool   # push grains ship False — explicit opt-in
	urgency: str          # "presence_routed" (default) | "always_push"
	source: str           # "tatva" (we send) | "core" (Frappe core sends — Phase 4)


GRAINS = [
	NotifGrain(
		key="Lead::Assignment::assigned",
		label="Lead assigned to me",
		description="A new lead is assigned to you.",
		automation_key="Notify::Lead::assigned",
		channels=("live",),
		default_optin=False,
		urgency="presence_routed",
		source="tatva",
	),
	NotifGrain(
		key="Task::Assignment::assigned",
		label="Task assigned to me",
		description="A new task is assigned to you.",
		automation_key="Notify::Task::assigned",
		channels=("live",),
		default_optin=False,
		urgency="presence_routed",
		source="tatva",
	),
]

_BY_KEY = {g.key: g for g in GRAINS}


def get(key: str) -> NotifGrain | None:
	"""Resolve a grain by key — None for an unknown key (dispatch fails closed on None)."""
	return _BY_KEY.get(key)


def all_grains() -> list:
	return list(GRAINS)
