"""The five authoring states and the legal moves between them — the ONE state machine a Workflow and a Task Form both obey.

Save is not publish, and publish is not activate (`docs/conventions/workflow/lifecycle-and-authoring.md`).
"""
import frappe
from frappe import _

LIFECYCLE_STATES = ("Draft", "Published", "Active", "Suspended", "Archived")
DRAFT, PUBLISHED, ACTIVE, SUSPENDED, ARCHIVED = LIFECYCLE_STATES

TRANSITIONS = {
	DRAFT: {PUBLISHED, ARCHIVED},
	PUBLISHED: {ACTIVE, DRAFT, ARCHIVED},
	ACTIVE: {SUSPENDED, DRAFT, ARCHIVED},
	SUSPENDED: {ACTIVE, DRAFT, ARCHIVED},
	ARCHIVED: set(),
}


# The verb that makes each move, as the builders and Desk name it; Draft from anywhere else is a Revise.
VERBS = {PUBLISHED: "publish", ACTIVE: "activate", SUSPENDED: "suspend", DRAFT: "revise", ARCHIVED: "archive"}


def moves(state):
	"""The legal moves out of `state` as `[{target, verb}]`, in lifecycle order: what an editor offers, never decides."""
	return [{"target": t, "verb": VERBS[t]} for t in LIFECYCLE_STATES if can_move(state, t)]


def is_editable(state):
	"""Authoring is a DRAFT-only operation; a released definition is frozen and Revised back to a Draft first."""
	return (state or DRAFT) == DRAFT


def can_move(state, target):
	return target in TRANSITIONS.get(state or DRAFT, set())


def assert_move(state, target, noun):
	"""Refuse an unknown state or an illegal move before any write; `noun` names the record ("workflow", "form")."""
	if target not in LIFECYCLE_STATES:
		frappe.throw(_("Unknown {0} state {1}.").format(noun, target))
	if not can_move(state, target):
		frappe.throw(
			_("A {0} {1} cannot become {2}.").format(state or DRAFT, noun, target),
			title=_("Not allowed"),
		)


def refuse(problems, title):
	"""Throw every blocking problem at once: the backstop a programmatic caller cannot ignore. An author's path reads them as data."""
	from tatva_connect.workflow_engine import registry

	blockers = registry.blocking(problems)
	if blockers:
		frappe.throw("<br>".join(frappe.utils.escape_html(p["message"]) for p in blockers), title=title)


def verdict(problems):
	"""Publish's answer when something blocks: `{ok, problems, summary}` as DATA, never a throw. None when nothing blocks."""
	from tatva_connect.workflow_engine import registry

	blockers = registry.blocking(problems)
	if blockers:
		# The refusal toast's one directive line, counted in the registry's problem taxonomy.
		return {"ok": False, "problems": problems, "summary": registry.problem_summary(blockers)}
	return None


def transition(doctype, name, target):
	"""One indirection over the controller's ONE state machine: load, permission-check, advance along a legal
	edge. `apply_transition` refuses an illegal edge before any write and runs the gate for that edge (Publish
	validates + freezes; Activate arms). No verb re-implements the machine."""
	doc = frappe.get_doc(doctype, name)
	doc.check_permission("write")
	state = doc.apply_transition(target)
	return {"name": doc.name, "lifecycle_state": state}


def publish(doctype, name):
	"""Draft -> Published: the whole release contract, then the freeze. A definition that is not ready is NOT an error: it is
	the expected state of authoring, so its problems come back as DATA and the editor marks what they name."""
	doc = frappe.get_doc(doctype, name)
	doc.check_permission("write")
	problems = doc.publish_problems()
	# A warns-only definition publishes; the warnings ride along so the editor can still surface them.
	return verdict(problems) or {"ok": True, "problems": problems, **transition(doctype, name, PUBLISHED)}
