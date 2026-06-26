# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""CaseSpec + the curated Tier-2 case list.

Each CaseSpec is one concrete (attack × principal × target × surface × action) point with a stable
id, an English description, the expected verdict (allow/deny), and the oracle to judge against
(defaults to the attack's oracle, overridable per case). The runner (test_registry_cases.py)
iterates these; the report and confusion matrix key on the id.

This is the SEED set covering the audit-critical vectors (permlevel field leak, the #4/#5
same-program trap, grain-vs-role, horizontal leak, cross-app deny, bypass-write). Expand by adding
rows — every attack key in attacks.py should end with >=1 case and >=1 planted mutation.
"""
from dataclasses import dataclass

from tatva_connect.tests.authz.registry.attacks import ATTACKS


@dataclass(frozen=True)
class CaseSpec:
	id: str
	attack: str            # key into ATTACKS
	english: str           # human description of what this case proves
	principal: str         # roster persona name (roster.PERSONAS[].persona) or a role string
	doctype: str
	action: str            # read | write | create | delete | field_read
	surface: str           # list | doc | field | bypass_write | method
	target: str            # in_grain | out_of_grain | same_program_diff_vertical | orphan_parent | wildcard | na
	expected: str          # "allow" | "deny"
	oracle: str = ""       # override; defaults to the attack's oracle

	def resolved_oracle(self):
		return self.oracle or ATTACKS[self.attack]["oracle"]


# Curated seed cases. ids are stable: <attack>-<principal>-<doctype-short>-<action>-<surface>.
CASES = [
	# A1 — horizontal grain leak: a grain user must see only their own grain's leads.
	CaseSpec("A1-grain4-lead-read-list", "A1",
	         "grain_4 (TatvaPractice/India/InsideSales) lists CRM Lead — sees only its grain's leads",
	         "grain_4", "CRM Lead", "read", "list", "in_grain", "allow"),
	CaseSpec("A1-grain4-othergrain-read-list", "A1",
	         "grain_4 must NOT see grain_1 (GoodFlip Care/Anaya) leads in the list",
	         "grain_4", "CRM Lead", "read", "list", "out_of_grain", "deny"),

	# A2 — THE trap: #4 and #5 share program 'InsideSales' but differ on vertical+group.
	CaseSpec("A2-grain4-grain5-read-list", "A2",
	         "grain_4 must NOT see grain_5 (GoodFlip/B2C/InsideSales) leads despite the shared "
	         "program name 'InsideSales' — matching keys on ALL axes, never program alone",
	         "grain_4", "CRM Lead", "read", "list", "same_program_diff_vertical", "deny"),

	# A4 — grain-vs-role contradiction: a CRM Lead Field Restriction must win over grain visibility.
	CaseSpec("A4-grain1-restricted-field", "A4",
	         "a field hidden from grain_1's role by CRM Lead Field Restriction stays hidden even "
	         "though the grain would otherwise show it",
	         "grain_1", "CRM Lead", "field_read", "field", "in_grain", "deny"),

	# A7 — permlevel field leak (audit C1): grain fields are permlevel-1; a user without permlevel-1
	# read must not receive custom_vertical/group/program via lead_detail or a Smart View column.
	CaseSpec("A7-grain1-permlevel-leaddetail", "A7",
	         "grain_1 (Sales User, no permlevel-1 read) must NOT receive custom_vertical/group/"
	         "current_program values from the lead_detail render surface",
	         "grain_1", "CRM Lead", "field_read", "field", "in_grain", "deny"),
	CaseSpec("A7-grain1-permlevel-smartview", "A7",
	         "the same permlevel-1 grain fields must NOT render as Smart View column values for "
	         "grain_1 — get_data must not bypass permlevel",
	         "grain_1", "CRM Lead", "field_read", "field", "in_grain", "deny"),

	# A6 — bypass-write escalation: partner mapped to grain_1 must not write an out-of-grain lead.
	CaseSpec("A6-partner-out-of-grain-write", "A6",
	         "partner (mapped to grain_1) writing a TatvaPractice lead is rejected BEFORE the "
	         "ignore_permissions save (partner out-of-grain raises DoesNotExistError)",
	         "partner", "CRM Lead", "write", "bypass_write", "out_of_grain", "deny"),

	# A11 — cross-app leak: the junk cross-app role must not reach CRM doctypes (the old VAPT class).
	CaseSpec("A11-junk-contact-read", "A11",
	         "junk_crossapp (Purchase Master Manager) must NOT read Contact (the regression the "
	         "original VAPT suite caught)",
	         "junk_crossapp", "Contact", "read", "doc", "na", "deny"),

	# A3 — vertical escalation: no_role must not gain any CRM Lead capability.
	CaseSpec("A3-norole-lead-write", "A3",
	         "no_role user has no CRM Lead write capability (baseline escalation guard)",
	         "no_role", "CRM Lead", "write", "doc", "na", "deny"),

	# A5 — reports_to roll-up: a manager's entitled grains must be ⊆ the union of reports' grains;
	# never wider than the manager's own native row visibility. (Vacuous on a bench without the
	# reports_to column — the mutation/self-validation declares that explicitly, never silent.)
	CaseSpec("A5-salesmgr-rollup-bounded", "A5",
	         "sales_manager's rolled-up grain visibility must not exceed the union of its reports' "
	         "grains, and never widen beyond native row visibility",
	         "sales_manager", "CRM Lead", "read", "list", "out_of_grain", "deny"),

	# A8 — orphan / forged parent: a child whose parent ref is missing/forged must be invisible.
	CaseSpec("A8-grain1-forged-parent-task", "A8",
	         "grain_1 must NOT read a CRM Task whose reference_docname is forged/orphaned "
	         "(fail-closed: no parent => not visible)",
	         "grain_1", "CRM Task", "read", "doc", "orphan_parent", "deny"),

	# A9 — switch-OFF exposure: with the visibility switch ON, an out-of-grain child is denied;
	# the OFF delta (stock CRM opens it) is documented and exercised in Playwright, not here.
	CaseSpec("A9-grain1-childscope-switch-on", "A9",
	         "with Task::CRM Task::visibility ON, grain_1 must NOT see an out-of-grain task; the "
	         "switch-OFF delta is the documented stock-CRM exposure",
	         "grain_1", "CRM Task", "read", "list", "out_of_grain", "deny"),

	# A10 — native-method bypass: a peer must be thrown by a guarded native method on another's row.
	CaseSpec("A10-peer-callog-method", "A10",
	         "a peer (grain_2) calling a guarded native method (e.g. get_recording_url) on a "
	         "CRM Call Log they cannot read is denied (PermissionError)",
	         "grain_2", "CRM Call Log", "read", "method", "out_of_grain", "deny"),

	# A12 — User Permission / DocShare over-grant: a fenced user sees only fenced rows; a share
	# grants only what was explicitly shared, never more.
	CaseSpec("A12-grain1-fenced-overreach", "A12",
	         "a User-Permission-fenced grain_1 must NOT see rows outside the fence, and an explicit "
	         "DocShare grants only the shared row",
	         "grain_1", "CRM Lead", "read", "list", "out_of_grain", "deny"),

	# A13 — partner-mapping abuse: a DISABLED mapping must deny the partner entirely (no leak).
	CaseSpec("A13-partner-disabled-mapping", "A13",
	         "with its CRM Lead API Mapping disabled, the partner is denied all access (no "
	         "wrong-tenant attribution, invariant 16)",
	         "partner", "CRM Lead", "read", "bypass_write", "in_grain", "deny"),
]


def all_cases():
	return list(CASES)


def cases_for(attack_key):
	return [c for c in CASES if c.attack == attack_key]


def attacks_without_cases():
	"""Attack keys that have no case yet — surfaced so coverage gaps are visible, never silent."""
	covered = {c.attack for c in CASES}
	return [k for k in ATTACKS if k not in covered]
