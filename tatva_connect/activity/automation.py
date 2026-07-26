"""Activity value reconstruction — the name the location backstop calls the ONE reader by.

`reconstruct_values` answers "what did this activity form say?", keyed by SCHEMA fieldname, for the
fail-closed location guard (`tasks.tasks.enforce_location` -> `location.api.location_required`).

It used to answer that itself: the JSON payload merged with `doc.get(f.target)` for every schema field
carrying a target. That was a second reader of a rule `activity.api._task_values` already owned, and it
carried the rule's PRE-Phase-5 shape — a retired slot column still holding an old value OVERRODE the
fresh answer, so a pre-Phase-5 task that is re-saved had its location judged on history. It now delegates
to `automation.context.activity_values`, which is the path the automation trigger context already takes
into `_task_values`; there is one reader, and it reads at the address `field_target` names.

The legacy task-type transition engine (apply_transitions / _apply_one) that also lived here was
folded into the unified automation engine — see db-seeds migration + patches.txt trace.
"""


def reconstruct_values(doc):
	"""The submitted activity form as {schema fieldname: value} — the ONE reader, reached the ONE way."""
	from tatva_connect.automation.context import activity_values

	return activity_values(doc)
