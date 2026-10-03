"""Frozen Task Form versions: the ONE brain on which form a task is read with, minted through the shared freeze (`authoring.versions`).

A task stamped with a version reads it. An unstamped task already answered reads the form's first version, the one the
migration froze from the live form it read until then. Anything else, a new or open task, reads the current version,
and a form never published reads its own record.
"""
import frappe
from frappe.model import NO_VALUE_FIELDS

from tatva_connect.access import request_cache
from tatva_connect.authoring import versions
from tatva_connect.tasks.tasks import DONE_STATUS

DOCTYPE = "CRM Task Type Version"
TASK_TYPE = "CRM Task Type"
# The column on CRM Task that remembers the version a task was answered on.
VERSION_FIELD = "custom_task_type_version"
# A form's lifecycle is a fact about its present, not its definition, so moving it never mints a version.
_STATE = ("enabled", "lifecycle_state")
_CACHE = "tatva_connect:task_form_version"


def _ready():
	"""Whether the versions exist on this site yet: between a deploy and its migrate a form reads its own record, as it did before."""
	return frappe.db.table_exists(DOCTYPE)


def task_columns():
	"""The version column, for a CRM Task read to select; empty until the fixture lands, so no read names a column that is not there."""
	return [VERSION_FIELD] if frappe.get_meta("CRM Task").has_field(VERSION_FIELD) else []


def build_payload(tt):
	"""The definition a rep's form is compiled from: the form's own columns and every child table its meta declares, in order."""
	head = {k: v for k, v in versions.freeze_row(tt).items() if k not in _STATE}
	tables = {df.fieldname: [versions.freeze_row(r) for r in tt.get(df.fieldname)] for df in tt.meta.get_table_fields()}
	return {**head, **tables}


def ensure_version(tt):
	"""Freeze `tt` as its current version (an unchanged form reuses its version) and return the version's name."""
	payload = build_payload(tt)
	questions = [r for r in tt.schema if (r.fieldtype or "") not in NO_VALUE_FIELDS]
	name = versions.mint(DOCTYPE, "task_type", tt.name, payload, question_count=len(questions))
	# The request's answers about which version is current are now stale.
	setattr(frappe.local, _CACHE, {})
	return name


# The two versions a reader asks for by name: the one served now, and the first, which tasks before versions read.
_WHICH = {"current": {"is_current": 1}, "first": {"version_no": 1}}


def _version(which, task_type):
	return request_cache(_CACHE, (which, task_type), lambda: frappe.db.get_value(
		DOCTYPE, {"task_type": task_type, **_WHICH[which]}) if _ready() else None)


def current_name(task_type):
	"""The version reps are offered and new tasks are answered on; None for a form never published."""
	return _version("current", task_type)


def first_name(task_type):
	"""The form's first version: what a task answered before versions existed was read with."""
	return _version("first", task_type)


def prime(task_types):
	"""Answer `current_name` and `first_name` for many forms in two reads, as `workflow_engine.versions.current_names` does:
	a picker, a task list and the lead rail ask per form, and the cost must not grow with the forms shown."""
	wanted = sorted({t for t in task_types if t})
	if not (wanted and _ready()):
		return
	for which, filters in _WHICH.items():
		found = dict(frappe.get_all(DOCTYPE, filters={"task_type": ["in", wanted], **filters}, fields=["task_type", "name"], as_list=True))
		for task_type in wanted:
			request_cache(_CACHE, (which, task_type), lambda t=task_type: found.get(t))


def version_of(task_type, task=None):
	"""The version name `task` (a name, a row or a doc) is read with, or the current one with no task; None if never published."""
	if task is not None:
		# A CRM Task is named by number, so a name is an int or a str, read once per request; a row or a doc is read as it is.
		row = request_cache(_CACHE, ("task", str(task)), lambda: frappe.db.get_value(
			"CRM Task", task, [*task_columns(), "status"], as_dict=True)) if isinstance(task, (str, int)) else task
		if row and row.get(VERSION_FIELD):
			return row.get(VERSION_FIELD)
		if row and row.get("status") == DONE_STATUS:
			return first_name(task_type) or current_name(task_type)
	return current_name(task_type)


def load(version_name):
	"""The frozen form as a CRM Task Type document, so every reader takes it exactly as it takes the live record."""
	def build():
		row = frappe.get_cached_doc(DOCTYPE, version_name)
		return frappe.get_doc({**frappe.parse_json(row.payload_json), "doctype": TASK_TYPE, "name": row.task_type})

	return request_cache(_CACHE, ("load", version_name), build)


def every_version(task_type):
	"""Every frozen form of `task_type`, newest first; a form never published is its live record alone."""
	names = frappe.get_all(DOCTYPE, filters={"task_type": task_type}, order_by="version_no desc", pluck="name") if _ready() else []
	return [load(name) for name in names] or [frappe.get_cached_doc(TASK_TYPE, task_type)]


def read(task_type, version_name):
	"""The form at `version_name`, or the live record of a form never published."""
	return load(version_name) if version_name else frappe.get_cached_doc(TASK_TYPE, task_type)


def form_of(task_type, task=None):
	"""The CRM Task Type `task` is read with (see the module docstring); with no task, the one a new task is answered on."""
	return read(task_type, version_of(task_type, task))
