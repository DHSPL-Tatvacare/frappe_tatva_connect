# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The listing declarations — what a rep sees on Leads, Tasks, Call Logs, Notes and Deals.

A column's label, its key, its width and WHICH columns exist are rules about what a resource's fields
are. They are ours, so they live here, in the app that owns the rules — not in the fork, whose Python is
upstream's and is not ours to edit. These five declarations were carried out of
`frappe_tatva_crm/crm/fcrm/doctype/**` verbatim on 2026-07-31 and the fork's four files returned to
frappe/crm v1.73.2 in the same change.

The seam is ONE hook. `override_doctype_class` (`hooks.py`) is read by `frappe.model.base_document.
get_controller` (`:110-122`), which is the single function all three consumers of this declaration
already call — the list payload (`crm/api/doc.py:301,338`), what a saved view is seeded from
(`crm_view_settings.py:174,195`) and the four rep pickers (`tatva_connect/api/task_lenses.py:45`). So one
entry per doctype redirects all three at once, with no fork edit and no post-hoc rewriting of a payload.
`get_controller` also enforces that an override subclasses the native class, which is what keeps every
other behaviour of these five doctypes exactly upstream's.

`default_kanban_settings` is deliberately NOT restated below. We never changed it, so it is upstream's
declaration and inheritance already answers it; a copy here would be a second brain that silently wins.

Each class carries its listing declaration and nothing else. Anything more belongs somewhere else — the
two assignment gates are MIXED IN from `lead/assignment.py` rather than written here, because a doctype
may have only one `override_doctype_class` entry and this file already holds it.

Plan: docs/plans/list-view-cleanup/2026-07-31-listing-lead-column-and-row-click.md §5.
"""

from crm.fcrm.doctype.crm_call_log.crm_call_log import CRMCallLog
from crm.fcrm.doctype.crm_deal.crm_deal import CRMDeal
from crm.fcrm.doctype.crm_lead.crm_lead import CRMLead
from crm.fcrm.doctype.crm_task.crm_task import CRMTask
from crm.fcrm.doctype.fcrm_note.fcrm_note import FCRMNote

from tatva_connect.lead.assignment import LeadAssignmentGate, TaskAssignmentGate


class TatvaCRMLead(LeadAssignmentGate, CRMLead):
	@staticmethod
	def default_list_data():
		# `name` is deliberately not a default column: a rep reads the person, support reads the id, and the picker still offers it because `rows` names it.
		columns = [
			{"label": "Full Name", "type": "Data", "key": "lead_name", "width": "12rem"},
			{"label": "Mobile No.", "type": "Data", "key": "mobile_no", "width": "11rem"},
			{
				"label": "Product Line",
				"type": "Link",
				"key": "custom_vertical",
				"options": "CRM Vertical",
				"width": "10rem",
			},
			{
				"label": "Group",
				"type": "Link",
				"key": "custom_group",
				"options": "CRM Group",
				"width": "9rem",
			},
			{
				"label": "Program",
				"type": "Link",
				"key": "custom_current_program",
				"options": "CRM Program",
				"width": "10rem",
			},
			{"label": "Created On", "type": "Datetime", "key": "creation", "width": "9rem"},
			{"label": "Modified On", "type": "Datetime", "key": "modified", "width": "9rem"},
			{"label": "Assigned To", "type": "Text", "key": "_assign", "width": "10rem"},
			{"label": "Lead Owner", "type": "Link", "key": "lead_owner", "options": "User", "width": "10rem"},
			{
				"label": "Status",
				"type": "Link",
				"key": "status",
				"options": "CRM Lead Status",
				"width": "9rem",
			},
			{
				"label": "Stage",
				"type": "Link",
				"key": "custom_stage",
				"options": "CRM Lead Stage",
				"width": "9rem",
			},
			{
				"label": "Sub-stage",
				"type": "Link",
				"key": "custom_substage",
				"options": "CRM Lead Stage",
				"width": "9rem",
			},
			{"label": "Temperature", "type": "Select", "key": "custom_lead_temperature", "width": "8rem"},
			{
				"label": "Source",
				"type": "Link",
				"key": "source",
				"options": "CRM Lead Source",
				"width": "9rem",
			},
			{"label": "Source Origin", "type": "Data", "key": "custom_source_origin", "width": "10rem"},
			{"label": "Email", "type": "Data", "key": "email", "width": "12rem"},
			{"label": "City", "type": "Data", "key": "custom_city", "width": "9rem"},
			{"label": "State", "type": "Data", "key": "custom_state", "width": "9rem"},
			{"label": "Gender", "type": "Link", "key": "gender", "options": "Gender", "width": "7rem"},
			{"label": "Patient ID", "type": "Data", "key": "custom_patient_id", "width": "10rem"},
			{
				"label": "Last Activity",
				"type": "Datetime",
				"key": "custom_prospectactivitydate_max",
				"width": "9rem",
			},
			{"label": "Last Report Date", "type": "Date", "key": "custom_last_report_date", "width": "9rem"},
			{"label": "Order Value", "type": "Currency", "key": "custom_order_value", "width": "9rem"},
			{"label": "SLA Status", "type": "Select", "key": "sla_status", "width": "9rem"},
		]
		rows = [
			"name",
			"mobile_no",
			"lead_name",
			"custom_vertical",
			"custom_group",
			"custom_current_program",
			"creation",
			"modified",
			"_assign",
			"lead_owner",
			"status",
			"custom_stage",
			"custom_substage",
			"custom_lead_temperature",
			"source",
			"custom_source_origin",
			"email",
			"custom_city",
			"custom_state",
			"gender",
			"custom_patient_id",
			"custom_prospectactivitydate_max",
			"custom_last_report_date",
			"custom_order_value",
			"sla_status",
			"first_name",
			"image",
		]
		return {"columns": columns, "rows": rows}


class TatvaCRMTask(TaskAssignmentGate, CRMTask):
	def before_insert(self):
		"""Stamp which half this row was born as — an APPOINTMENT someone promised, or a RECORD of
		something already done. The modal shows its scheduling half iff the answer is the former.

		Derived from the only evidence that is always true at insert: whether a due date arrived with the
		row. New Task requires one, the automation follow-up computes one, and Log Activity sends none.
		Stored rather than re-derived, because a rep clearing the date later would otherwise turn a kept
		appointment into a bare record and the task would lose the reason it existed.

		Overwritten, never read from the caller: a client that could assert this would hand itself
		scheduling controls for an appointment nobody made. An invariant, not an operator toggle, so it
		binds on the controller beside the auth overrides rather than in `doc_events` — there is no state
		of this system in which a row may be born not knowing which half it is."""
		super_before = getattr(super(), "before_insert", None)
		if super_before:
			super_before()
		self.custom_is_planned = 1 if self.due_date else 0

	@staticmethod
	def default_list_data():
		columns = [
			{"label": "Task ID", "type": "Data", "key": "name", "width": "10rem"},
			{
				"label": "Lead",
				"type": "Dynamic Link",
				"key": "reference_docname",
				"options": "reference_doctype",
				"width": "11rem",
			},
			{"label": "Title", "type": "Data", "key": "title", "width": "16rem"},
			{
				"label": "Task Type",
				"type": "Link",
				"key": "custom_task_type",
				"options": "CRM Task Type",
				"width": "10rem",
			},
			{"label": "Status", "type": "Select", "key": "status", "width": "8rem"},
			{"label": "Priority", "type": "Select", "key": "priority", "width": "8rem"},
			{"label": "Due Date", "type": "Datetime", "key": "due_date", "width": "9rem"},
			{
				"label": "Assigned To",
				"type": "Link",
				"key": "assigned_to",
				"options": "User",
				"width": "10rem",
			},
			{"label": "Completed On", "type": "Date", "key": "custom_completed_on", "width": "9rem"},
			{"label": "Created On", "type": "Datetime", "key": "creation", "width": "9rem"},
			{"label": "Modified On", "type": "Datetime", "key": "modified", "width": "9rem"},
		]

		# The ONE declaration of the rep-facing field set — filter, group-by, sort and the column picker all resolve through it (tatva_connect/api/task_lenses.py).
		rows = [
			"name",
			"reference_doctype",
			"reference_docname",
			"title",
			"custom_task_type",
			"status",
			"priority",
			"due_date",
			"start_date",
			"assigned_to",
			"custom_completed_on",
			"custom_outcome",
			"custom_followup_at",
			"custom_scheduled_at",
			"creation",
			"modified",
			"description",
		]
		return {"columns": columns, "rows": rows}


class TatvaCRMCallLog(CRMCallLog):
	@staticmethod
	def default_list_data():
		columns = [
			{"label": "Call ID", "type": "Data", "key": "name", "width": "10rem"},
			{
				"label": "Lead",
				"type": "Dynamic Link",
				"key": "reference_docname",
				"options": "reference_doctype",
				"width": "11rem",
			},
			{"label": "Type", "type": "Select", "key": "type", "width": "8rem"},
			{"label": "Status", "type": "Select", "key": "status", "width": "8rem"},
			{"label": "From (number)", "type": "Data", "key": "from", "width": "9rem"},
			{"label": "To (number)", "type": "Data", "key": "to", "width": "9rem"},
			{"label": "Caller", "type": "Link", "key": "caller", "options": "User", "width": "9rem"},
			{"label": "Received By", "type": "Link", "key": "receiver", "options": "User", "width": "9rem"},
			{"label": "Duration", "type": "Duration", "key": "duration", "width": "6rem"},
			{"label": "Start Time", "type": "Datetime", "key": "start_time", "width": "9rem"},
			{"label": "End Time", "type": "Datetime", "key": "end_time", "width": "9rem"},
			{"label": "Medium", "type": "Select", "key": "telephony_medium", "width": "8rem"},
			{
				"label": "Account",
				"type": "Link",
				"key": "custom_telephony_account",
				"options": "CRM Telephony Account",
				"width": "10rem",
			},
			{"label": "Created On", "type": "Datetime", "key": "creation", "width": "9rem"},
		]
		rows = [
			"name",
			"reference_doctype",
			"reference_docname",
			"type",
			"status",
			"from",
			"to",
			"caller",
			"receiver",
			"duration",
			"start_time",
			"end_time",
			"telephony_medium",
			"custom_telephony_account",
			"creation",
			"note",
			"recording_url",
		]
		return {"columns": columns, "rows": rows}


class TatvaFCRMNote(FCRMNote):
	# Upstream answers with NO columns at all, which is why the Notes page grew a hardcoded copy in the browser; the server owns them from here and that fallback stops firing on its own.
	@staticmethod
	def default_list_data():
		columns = [
			{"label": "Note ID", "type": "Data", "key": "name", "width": "10rem"},
			# The lead sits SECOND by default — a note is read as "whose note is this" before "what does it say". This is only the default: a saved view always wins, so a rep who reorders is never overridden.
			{
				"label": "Lead",
				"type": "Dynamic Link",
				"key": "reference_docname",
				"options": "reference_doctype",
				"width": "11rem",
			},
			{"label": "Title", "type": "Data", "key": "title", "width": "16rem"},
			# Text Editor, not Text: NotesListView renders THIS type through sanitizeHTML, and a plain Text prints the note's raw <p> markup on screen.
			{"label": "Content", "type": "Text Editor", "key": "content", "width": "22rem"},
			{"label": "Created By", "type": "Link", "key": "owner", "options": "User", "width": "10rem"},
			{"label": "Last Modified", "type": "Datetime", "key": "modified", "width": "9rem"},
		]
		rows = [
			"name",
			"title",
			"content",
			"reference_doctype",
			"reference_docname",
			"owner",
			"modified",
		]
		return {"columns": columns, "rows": rows}


class TatvaCRMDeal(CRMDeal):
	@staticmethod
	def default_list_data():
		columns = [
			{"label": "Deal ID", "type": "Data", "key": "name", "width": "10rem"},
			{"label": "Lead ID", "type": "Link", "key": "lead", "options": "CRM Lead", "width": "11rem"},
			{"label": "Lead Name", "type": "Data", "key": "lead_name", "width": "12rem"},
			{
				"label": "Organization",
				"type": "Link",
				"key": "organization",
				"options": "CRM Organization",
				"width": "11rem",
			},
			{
				"label": "Status",
				"type": "Link",
				"key": "status",
				"options": "CRM Deal Status",
				"width": "10rem",
			},
			{"label": "Deal Owner", "type": "Link", "key": "deal_owner", "options": "User", "width": "10rem"},
			{
				"label": "Deal Value",
				"type": "Currency",
				"key": "deal_value",
				"align": "right",
				"width": "9rem",
			},
			{
				"label": "Expected Value",
				"type": "Currency",
				"key": "expected_deal_value",
				"align": "right",
				"width": "9rem",
			},
			{"label": "Expected Closure", "type": "Date", "key": "expected_closure_date", "width": "9rem"},
			{"label": "Probability", "type": "Percent", "key": "probability", "width": "8rem"},
			{
				"label": "Source",
				"type": "Link",
				"key": "source",
				"options": "CRM Lead Source",
				"width": "9rem",
			},
			{"label": "Mobile No.", "type": "Data", "key": "mobile_no", "width": "11rem"},
			{"label": "Email", "type": "Data", "key": "email", "width": "12rem"},
			{"label": "Created On", "type": "Datetime", "key": "creation", "width": "9rem"},
			{"label": "Modified On", "type": "Datetime", "key": "modified", "width": "9rem"},
		]
		rows = [
			"name",
			"lead",
			"lead_name",
			"organization",
			"status",
			"deal_owner",
			"deal_value",
			"expected_deal_value",
			"expected_closure_date",
			"probability",
			"source",
			"mobile_no",
			"email",
			"creation",
			"modified",
			"currency",
			"_assign",
		]
		return {"columns": columns, "rows": rows}
