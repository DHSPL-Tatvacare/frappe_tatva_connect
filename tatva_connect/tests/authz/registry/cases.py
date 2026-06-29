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

	# A7 — grain fields are READ-allowed but EDIT-denied for a grain user. The intended model (confirmed
	# 2026-06-29): a Sales User SEES which grain a lead belongs to, but only a manager / the assignment-
	# rule stage may MOVE it. vertical/group are permlevel-1 (structurally unwritable by a Sales User);
	# current_program is permlevel-0 but the grain controller rejects an out-of-entitlement save when the
	# grain switch is ON (switch OFF = the documented stock exposure, like the child-visibility switches).
	# The field READ-leak to a principal WITHOUT permlevel-1 read is the negative control proven in
	# mutation.py (a fresh permlevel-0 role), not a case here — no roster persona has Lead read yet lacks
	# permlevel-1 read.
	CaseSpec("A7-grain1-reads-own-grain-fields", "A7",
	         "grain_1 (Sales User) CAN read its own lead's grain fields (vertical/group/program) — "
	         "intended, NOT a leak; the protection is on EDIT, not read",
	         "grain_1", "CRM Lead", "field_read", "field", "in_grain", "allow"),
	CaseSpec("A7-grain1-cannot-move-lead-out-of-grain", "A7",
	         "grain_1 CANNOT move a lead to a program outside its entitlement (grain edit-denied; only a "
	         "manager / the assignment-rule stage may change grain) — enforced when the grain switch is ON",
	         "grain_1", "CRM Lead", "write", "field", "in_grain", "deny"),

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
	# A13 — partner CALL/ACTIVITY create against an OUT-OF-GRAIN lead: resolve_lead's forced grain
	# filter raises (DoesNotExistError) BEFORE the ignore_permissions save, so the partner (grain_1)
	# can never plant a child on a grain_3 lead.
	CaseSpec("A13-partner-callog-out-of-grain-create", "A13",
	         "partner (grain_1) creating a CRM Call Log against a grain_3 lead is rejected by "
	         "resolve_lead's forced grain filter before the ignore_permissions save",
	         "partner", "CRM Call Log", "create", "bypass_write", "out_of_grain", "deny"),
	CaseSpec("A13-partner-activity-out-of-grain-create", "A13",
	         "partner (grain_1) creating a CRM Task (activity) against a grain_3 lead is rejected by "
	         "resolve_lead's forced grain filter before the ignore_permissions save",
	         "partner", "CRM Task", "create", "bypass_write", "out_of_grain", "deny"),
	# A13 — external_id is a PER-PARTNER namespace, not global: a colliding external_id already on a
	# grain_3 lead's row must NOT resolve/overwrite that row for the grain_1 partner (the cross-tenant fix).
	CaseSpec("A13-partner-extid-collision-no-cross-tenant", "A13",
	         "partner (grain_1) sending an external_id that already exists on a grain_3 lead's CRM "
	         "Call Log must NOT resolve that row — find_by_external_id_scoped returns None (grain-scoped "
	         "via the linked lead), so a colliding id can never overwrite another tenant's row",
	         "partner", "CRM Call Log", "create", "bypass_write", "out_of_grain", "deny"),

	# A14 — public-intake guest abuse: an anonymous web-form submit must not escape the form's grain.
	CaseSpec("A14-guest-routing-forced", "A14",
	         "a Guest submission carrying foreign custom_vertical/group/program lands on the FORM's "
	         "grain (forced routing), never the smuggled one — is_sysmgr=False+mp drops submitter routing",
	         "Guest", "CRM Lead", "write", "bypass_write", "out_of_grain", "deny"),
	CaseSpec("A14-guest-no-master-growth", "A14",
	         "a Guest manual-field submit into a GROWABLE master (CRM Side Effect Option — not "
	         "pick-only, not grain-scoped) returns canonical text and grows NO row, while the SAME "
	         "_ensure_master call as an authed user grows it by 1 — the differential proves the block "
	         "is the Guest guard (intake.py:355), not a universal/pick-only one",
	         "Guest", "CRM Side Effect Option", "create", "bypass_write", "na", "deny"),
	CaseSpec("A14-guest-note-scope", "A14",
	         "a Guest fold writes its FCRM Note ONLY against the form's own resolved lead "
	         "(reference_docname == the fold's lead) — never another lead",
	         "Guest", "FCRM Note", "create", "bypass_write", "in_grain", "allow"),

	# A7 (Smart View column leak): a grain user authoring a Smart View cannot project a column outside
	# the view's grain catalog — _validate_columns is a fail-closed allowlist (any non-catalog key throws).
	CaseSpec("A7-grain1-smartview-out-of-grain-column", "A7",
	         "grain_1 (Sales User) calling upsert_view with a column that is not in its grain's catalog "
	         "is rejected by _validate_columns (fail-closed allowlist) before the view is saved",
	         "grain_1", "CRM Smart View", "create", "smartview", "out_of_grain", "deny"),

	# A12 (Smart View grain clamp): a grain user cannot author a view scoped to a grain it isn't entitled
	# to — _grains_from_axes clamps explicit axes to entitlement and raises PermissionError fail-closed.
	CaseSpec("A12-grain1-smartview-grain-clamp", "A12",
	         "grain_1 (Sales User) calling upsert_view with an out-of-grain (vertical,group,program) "
	         "axis is rejected by _grains_from_axes (the cross-tenant write clamp) with PermissionError",
	         "grain_1", "CRM Smart View", "create", "smartview", "out_of_grain", "deny"),
]


def all_cases():
	return list(CASES)


def cases_for(attack_key):
	return [c for c in CASES if c.attack == attack_key]


def attacks_without_cases():
	"""Attack keys that have no case yet — surfaced so coverage gaps are visible, never silent."""
	covered = {c.attack for c in CASES}
	return [k for k in ATTACKS if k not in covered]
