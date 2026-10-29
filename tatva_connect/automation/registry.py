"""The ONE catalog — every automation in `tatva_connect` is one row here.

Every automation is an operator toggle, gated via `is_enabled(key)`: ON -> the
tatva_connect hook rides and enforces; OFF -> it's dormant and native frappe/crm
behaviour stands. No locked/always-on class — the operator owns every `enabled`.

`fires_on`: Doc Event · Schedule · Provider call · Permission.
`Area` is derived — segment 1 of the composite key (`key.split("::")[0]`).
`purpose`: two to four short sentences, the same shape for every row: what the feature does
while on, then what happens while it is off, in the house writing style (docs/conventions/writing).
Third person, no "you". Seeded into `description` on migrate and shown as the flag form's headline.
`backs` = the real dotted paths the row covers; the drift check (drift.py) matches
hooks.py against these strings, so a new doc_event/scheduler path with no registry
row fails `bench migrate`.
"""
from dataclasses import dataclass, field

KEY_SHAPE = "Area::Subject::Capability"


def assert_valid_key(key):
	"""The ONE shape check for an automation key — exactly three non-empty `::` parts."""
	parts = (key or "").split("::")
	if len(parts) != 3 or not all(part.strip() for part in parts):
		raise ValueError(
			f"Automation key {key!r} must be {KEY_SHAPE} — exactly three non-empty `::` segments. "
			"`Area` is derived from segment 1, and the go-live checklist groups by it."
		)
	return key


def assert_valid_graph(autos):
	"""The ONE graph check — every `requires` names a declared key, and no chain of them loops."""
	parents = {auto.key: auto.requires for auto in autos}
	for key, parent in parents.items():
		if parent and parent not in parents:
			raise ValueError(
				f"Automation {key!r} requires {parent!r}, which no row declares. "
				"A `requires` must name a key in this same catalog."
			)
	for key in parents:
		chain = [key]
		parent = parents[key]
		while parent:
			if parent in chain:
				raise ValueError(
					f"Automation hierarchy is cyclic: {' -> '.join([*chain, parent])}. "
					"A `requires` chain must end at a row that requires nothing."
				)
			chain.append(parent)
			parent = parents[parent]
	return parents


@dataclass(frozen=True)
class Auto:
	key: str
	fires_on: str
	trigger_detail: str = ""
	purpose: str = ""
	backs: list = field(default_factory=list)
	requires: str = ""
	activator: str = ""
	guard: bool = False  # a data check, not a side effect: it runs in every Bulk Lane, Quiet included

	# Import-time shape gate: a malformed key cannot reach the catalog, so drift fails the build.
	def __post_init__(self):
		assert_valid_key(self.key)
		if self.requires:
			assert_valid_key(self.requires)


AUTOMATIONS = [
	Auto(
		key="AI Voice::Channel::calls",
		fires_on="Provider call",
		trigger_detail="automation/sends.send_voice gate · webhooks/spine kill-switch · voice ingress",
		purpose=(
			"Lets a journey place AI voice calls to a lead through the voice account named on its step. "
			"The outcome of each call comes back and picks the next step. When this is off, no call is "
			"placed and the journey takes its failed path. Outcomes that arrive are logged and can be "
			"replayed later."
		),
		# Both directions are gated by this ONE row, read through voice/channel.py: outbound in
		# `sends.send_voice`, inbound as the spine's `enabled` callback. Not a doc_event on either side,
		# so `backs` is empty — the same shape the WhatsApp channel row has.
		backs=[],
	),
	Auto(
		key="AI Voice::Channel::reconcile",
		fires_on="Schedule",
		trigger_detail="every 15m · polls the provider for calls whose outcome never arrived",
		purpose=(
			"Every 15 minutes, checks with the voice provider on calls whose outcome never came back. The "
			"journey waiting on that call then moves on. When this is off, only the provider's own "
			"callback can move such a journey, so a lost callback leaves it stuck."
		),
		backs=["tatva_connect.voice.reconcile.sweep"],
		requires="AI Voice::Channel::calls",
	),
	Auto(
		key="Storage::Recording::retry",
		fires_on="Schedule",
		trigger_detail="every 15m · retries call recordings whose download failed, with backoff",
		purpose=(
			"Fetches a call recording again when the first download failed. Each call gets a few more "
			"tries over about a day, then the reason is kept on its record. When this is off, a failed "
			"recording stays missing and the call shows no audio."
		),
		backs=[
			"tatva_connect.storage.call_media.sweep",
			# The other half of the same layer's housekeeping: a deleted call takes its media pointer with it, so nothing accumulates.
			"tatva_connect.storage.call_media.drop_for_call",
		],
	),
	Auto(
		key="Transcription::Channel::inbound",
		fires_on="Provider call",
		trigger_detail="webhooks/spine kill-switch · transcription ingress",
		purpose=(
			"Lets a transcription service send the text of a recorded call back to the CRM. The text is "
			"saved on the call it belongs to. When this is off, a sent transcript is logged but not "
			"saved, and it can be replayed once this is on."
		),
		backs=[],
	),
	Auto(
		key="Notifications::Tray::retention",
		fires_on="Schedule",
		trigger_detail="operator-armed · daily purge of read tray rows",
		purpose=(
			"Once a day, deletes bell notifications a rep has read once they are 90 days old. Unread ones "
			"are never deleted, however old. When this is off, every notification stays in the bell and "
			"the list keeps growing."
		),
		backs=["tatva_connect.notifications.retention.purge_read_notifications"],
	),
	Auto(
		key="WhatsApp::Channel::messaging",
		fires_on="Provider call",
		trigger_detail="whatsapp/api gate · WhatsApp Message · before_save",
		purpose=(
			"Turns on WhatsApp for leads. Messages and templates go out, and patient replies come back, "
			"through the WhatsApp account routed to the lead's business line. When this is off, nothing "
			"is sent. Incoming messages are logged but not filed on any lead, and can be replayed later."
		),
		backs=["tatva_connect.whatsapp.webhook.pin_inbound_reference"],
	),
	Auto(
		key="WhatsApp::Channel::templates",
		fires_on="Schedule",
		trigger_detail="every 6h",
		purpose=(
			"Every six hours, pulls the list of approved WhatsApp templates from the provider for each "
			"active account. Reps then always pick from the current list. When this is off, the list "
			"stays as the last sync left it until someone syncs it by hand."
		),
		backs=["tatva_connect.whatsapp.templates_sync.scheduled_sync_all"],
		requires="WhatsApp::Channel::messaging",
	),
	Auto(
		key="WhatsApp::Channel::recovery",
		fires_on="Provider call",
		trigger_detail="orphan status · one-message conversation read",
		purpose=(
			"When a delivery or read receipt arrives for a WhatsApp message the CRM never saved, fetches "
			"that one message from the provider. The message is filed on its lead and the receipt "
			"applied. This works only for a provider that supports it. When this is off, such a receipt "
			"is logged and dropped."
		),
		backs=[],
		requires="WhatsApp::Channel::messaging",
	),
	Auto(
		key="WhatsApp::Channel::media-retry",
		fires_on="Schedule",
		trigger_detail="every 15 min · owed media, backoff over ~1 day",
		purpose=(
			"Asks the provider again for a photo or document that failed to download when its WhatsApp "
			"message arrived. Each message gets a fixed number of tries over about a day. When this is "
			"off, the message keeps its Media unavailable note for good."
		),
		backs=["tatva_connect.whatsapp.media_retry.sweep"],
		requires="WhatsApp::Channel::messaging",
	),
	Auto(
		key="WhatsApp::Channel::enrolment",
		fires_on="Provider call",
		trigger_detail="live inbound only · unknown sender · per-account tick",
		purpose=(
			"Creates a lead when someone not yet in the CRM sends a message to a WhatsApp number. The "
			"lead is filed under the business line that number serves. It works only on accounts ticked "
			"for it, and only for live messages. When this is off, a message from an unknown sender is "
			"dropped."
		),
		backs=[],
		requires="WhatsApp::Channel::messaging",
	),
	Auto(
		key="Telephony::Channel::calls",
		fires_on="Provider call",
		trigger_detail="telephony/api gate",
		purpose=(
			"Turns on click-to-call and call logging through the phone line routed to the lead's business "
			"line. When this is off, the call button is hidden and a lead cannot be dialled from the CRM. "
			"Call records the provider sends are logged but not filed."
		),
		backs=[],
	),
	Auto(
		key="Storage::Azure::offload",
		fires_on="Doc Event",
		trigger_detail="File · after_insert + on_trash",
		purpose=(
			"Moves every new attachment off the app server into Azure storage, and the CRM serves it from "
			"there. When this is off, new files stay on the app server. A file already moved to Azure is "
			"still removed from Azure when it is deleted."
		),
		# link_attach_fields is part of the offload's contract, not a switch of its own: an offloaded URL is
		# remote, so core's linker declines it and the file<->record bond is ours to make.
		backs=[
			"tatva_connect.storage.file_events.after_insert",
			"tatva_connect.storage.file_events.on_trash",
			"tatva_connect.storage.file_events.link_attach_fields",
		],
	),
	Auto(
		key="Location::Google::capture",
		fires_on="Provider call",
		trigger_detail="location/api gate · CRM Lead after_insert",
		purpose=(
			"Checks a rep's location when they close an in-person visit on a tracked business line. A "
			"visit too far from the doctor's pinned spot is blocked, and each check is logged. A new "
			"doctor is pinned where the rep created them. When this is off, no location is taken or "
			"checked."
		),
		backs=["tatva_connect.location.api.capture_on_create"],
	),
	Auto(
		key="Location::NearMe::directory",
		fires_on="Provider call",
		trigger_detail="near_me/api gate",
		purpose=(
			"Opens the Near Me page, a map and list of every lead with a pinned clinic across all "
			"business lines. A rep in the field can find nearby doctors and call them. Only users with "
			"the Field Map User role see it, and when this is off, the page is hidden from everyone."
		),
		backs=[],
	),
	Auto(
		key="Lead::Assignment::owner",
		fires_on="Doc Event",
		trigger_detail="CRM Lead · after_insert + validate (lead_owner changed)",
		purpose=(
			"When someone sets the owner on a lead, the lead is assigned to that person and shared with "
			"them, so it shows in their list. When this is off, the owner field still keeps the name, but "
			"no assignment or share is made. Leads with no owner are not affected either way."
		),
		backs=[],  # the fork assigns off a field in crm_lead.py, not a doc_event; gated by lead/assignment.LeadAssignmentGate on the override class
	),
	Auto(
		key="Lead::Assignment::from_rule",
		fires_on="Doc Event",
		trigger_detail="ToDo · after_insert (reference_type = CRM Lead)",
		purpose=(
			"When an assignment rule gives a lead with no owner to a rep, that rep becomes the lead "
			"owner. When a person assigns a lead to someone, the owner moves to that person too. When "
			"this is off, the assignment still stands, but the owner field stays as it was."
		),
		# on_lead_reassignment_handover rides the same ToDo.after_insert — always on, no switch of its own (task handover is a correctness fix, not an optional automation), covered here so the drift lock passes.
		backs=[
			"tatva_connect.lead.assignment.on_assignment_set_owner",
			"tatva_connect.tasks.tasks.on_lead_reassignment_handover",
			# The share withdrawals are the other half of an assignment (ToDo · on_update, CRM Lead · on_update): always on, no switch of their own.
			"tatva_connect.lead.assignment.withdraw_share",
			"tatva_connect.lead.assignment.withdraw_owner_share",
			# A pool's holiday-list map drops on CRM Holiday List save and delete (crm-owned, so a doc_event): always on, no switch of its own.
			"tatva_connect.lead.assignment_rule.clear_pool_map",
			# A pool pick re-reads the rep's check-in card in their open tabs (ToDo): a UI nudge, always on, no switch of its own.
			"tatva_connect.lead.checkin.on_pool_assignment",
			# A live working record (presence.working: a call in progress) keeps its agent present: no switch of its own, inert while Auto-Away After is blank.
			"tatva_connect.presence.on_working_doc",
		],
	),
	Auto(
		key="Jira::Issues::raise",
		fires_on="Provider call",
		trigger_detail="Record header · Raise Jira issue; HD Ticket · on_update (ticket closed)",
		purpose=(
			"Lets agents and reps raise a Jira issue from a ticket or a lead, linked back to the record inside "
			"Jira, and shows the issue's live status on the ticket. A ticket that closes shows as resolved on its "
			"issue. Needs the Jira tab of Tatva Record Actions filled in. When this is off, no record offers it."
		),
		backs=["tatva_connect.record_actions.jira.on_ticket_closed"],
	),
	Auto(
		key="Checkin::Status::automatic",
		fires_on="Doc Event",
		trigger_detail="Tatva Activity Log · after_insert (Check-in rows); HD Agent · on_update (availability changed); daily 00:01",
		purpose=(
			"The system keeps each person's status for them. One status across the CRM and Helpdesk: "
			"checking in, going away or checking out in either app shows in the other. And just after "
			"midnight, everyone still checked in is checked out, unless one of their shifts is open then. "
			"When this is off, each app keeps its own status and nobody is checked out at midnight."
		),
		backs=["tatva_connect.lead.checkin.from_helpdesk", "tatva_connect.lead.checkin.close_the_day"],
	),
	# RETIRED 2026-10-29 — Lead::Checkin::helpdesk and Lead::Checkin::midnight. Both keep a person's status, not a lead's, so they are one switch: Checkin::Status::automatic. Never in prod.
	# RETIRED 2026-10-24 — Notifications::Email::deep-links. Where a notice opens is one link every channel reads (bell, email, push), so it is part of Notify::Tatva::live, not a second switch.
	# RETIRED 2026-09-28 — Task::Assignment::assignee. A task is held by its `assigned_to` column: its list, filter, visibility, notifications and lead handover all read that column, and the mirroring ToDo it switched on served none of them. Off in prod since go-live.
	# RETIRED 2026-08-31 — Task::Assignment::followup. Predated the workflow engine's own Create Task node (2026-08-10) by two months, which does the same job authored per program instead of one hardcoded rule; the go-live checklist always listed it under switches to confirm OFF, never one to turn on.
	Auto(
		key="Task::Review::mirror",
		fires_on="Doc Event",
		trigger_detail="CRM Task · on_update",
		purpose=(
			"Copies the Approved or Rejected verdict from a Document Review task onto the file it "
			"reviewed. The file then shows that badge on the lead's Attachments tab. When this is off, "
			"the verdict stays on the task and the file's badge does not change."
		),
		backs=[
			"tatva_connect.tasks.review_mirror.mirror_review_outcome",
		],
	),
	Auto(
		key="Storage::File::draft-cleanup",
		fires_on="Schedule",
		trigger_detail="daily 02:30",
		purpose=(
			"Each night, deletes files that were added to an email draft and are still not attached to "
			"any record a day later. A copy that a sent email shares stays safe. When this is off, these "
			"leftover files are kept forever."
		),
		backs=["tatva_connect.api.email.purge_draft_attachments"],
	),
	Auto(
		key="Partner::Idempotency::cleanup",
		fires_on="Schedule",
		trigger_detail="daily 04:00",
		purpose=(
			"Each night, deletes the saved answers that let a partner safely retry a request. It removes "
			"only those older than the Idempotency Window in Partner API Settings. When this is off, the "
			"saved answers are kept forever and keep piling up."
		),
		backs=["tatva_connect.api._base.purge_idempotency_keys"],
	),
	Auto(
		key="Notify::Tatva::live",
		fires_on="Doc Event",
		trigger_detail="CRM Call Log saves · task reminders every 5 minutes · push every minute",
		purpose=(
			"Turns on the missed-call, task-due-soon and task-overdue notices, push alerts for every notice "
			"once CRM Push Settings is filled, and links that open each notice in the app (a lead, ticket or "
			"filtered list) from the bell, the email and the push alike. Each rep picks, in their own "
			"Notification Settings, which notices reach email and push. A bulk action (an import, a partner bulk "
			"job, a list action, a user separation, a scheduled workflow), and every workflow it starts, tells each person once per wave instead of once per record, unless it ticked Don't "
			"Notify Assignees. Inside the quiet hours a person sets for themselves, nothing is pushed and their "
			"email waits until the hours end. When this is off, none of the three is written, nothing is pushed, "
			"quiet hours hold nothing, and an email links to the Desk form."
		),
		backs=[
			"tatva_connect.notifications.events.on_call_missed",
			"tatva_connect.notifications.events.sweep_reminders",
			"tatva_connect.notifications.events.reset_reminder_stamps",
			"tatva_connect.notifications.push.drain",
			"tatva_connect.notifications.push.mark_pending",
			"tatva_connect.notifications.deep_link.point_at_the_app",
			"tatva_connect.notifications.batch.flush",
			"tatva_connect.notifications.batch.stamp",
			"tatva_connect.notifications.quiet_hours.hold_email",
		],
	),
	Auto(
		key="Lead::CRM Lead::dedup",
		guard=True,
		fires_on="Doc Event",
		trigger_detail="CRM Lead · before_validate + validate",
		purpose=(
			"Saves every phone number on a lead in one standard format and refuses a number that is not "
			"real. It also blocks a second lead with the same mobile number in the same business line and "
			"group. When this is off, numbers save as typed and duplicate leads are allowed."
		),
		# Phone + routing canonicalisation ride WITH dedup, not as separate switches:
		# the dedup anchor compares on the canonical form, so dedup silently misses
		# duplicates without them — one mechanism, one toggle.
		backs=[
			"tatva_connect.lead.leads.canonicalize_routing_fields",
			"tatva_connect.lead.leads.normalize_lead_phones",
			"tatva_connect.lead.leads.dedup_guard",
		],
	),
	Auto(
		key="Lead::CRM Lead::grain",
		guard=True,
		fires_on="Doc Event",
		trigger_detail="CRM Lead · before_validate",
		purpose=(
			"Files each lead under the business line, group and program its creator works in. A user with "
			"one program gets it filled in, and a manager's pick is checked against what they may access. "
			"When this is off, whatever is typed is saved without a check."
		),
		backs=["tatva_connect.lead.leads.stamp_entitled_grain"],
	),
	Auto(
		key="Lead::CRM Lead::stage",
		guard=True,
		fires_on="Doc Event",
		trigger_detail="CRM Lead · validate",
		purpose=(
			"Lets a lead sit only at stages that belong to its program, and fills in the main stage from "
			"the stage picked. A save with a stage from another program is blocked with a clear message. "
			"When this is off, any stage can be set on any lead."
		),
		backs=["tatva_connect.lead.leads.validate_stage"],
	),
	Auto(
		key="Deal::CRM Deal::guards",
		guard=True,
		fires_on="Doc Event",
		trigger_detail="CRM Deal · before_validate + validate",
		purpose=(
			"Ties each deal to its lead, copies the lead's business line onto it, and works out renewal "
			"dates from each plan's length. It refuses a deal when the line does not allow deals, the "
			"lead is not at its buying stage, or the lead already has one. When this is off, none of "
			"these checks run."
		),
		backs=[
			"tatva_connect.deal.deals.require_lead",
			"tatva_connect.deal.deals.stamp_grain_from_lead",
			"tatva_connect.deal.deals.guard_deals_enabled",
			"tatva_connect.deal.deals.guard_conversion_point",
			"tatva_connect.deal.deals.one_deal_per_lead",
			"tatva_connect.deal.deals.stamp_renewal_dates",
			"tatva_connect.deal.deals.normalize_deal_phones",
		],
	),
	Auto(
		key="Lead::Meta::conversions",
		fires_on="Schedule",
		trigger_detail="every 15m, hourly or daily per CRM Meta Dataset · one queued send per dataset on the long lane",
		purpose=(
			"Sends stage changes on Facebook leads back to Meta, so Meta learns which leads convert. Each "
			"enabled Meta dataset sends the stages it maps on its own schedule, with only the Meta lead "
			"ID. When this is off, nothing is sent, and changes older than seven days are skipped once it "
			"is back on."
		),
		backs=[
			"tatva_connect.lead_sync.conversions.send_every_15_minutes",
			"tatva_connect.lead_sync.conversions.send_hourly",
			"tatva_connect.lead_sync.conversions.send_daily",
		],
	),
	# RETIRED 2026-09-22 — Lead::Facebook::form-refresh. A nightly re-run of the discovery that saving a source and the Refresh button already run, saving one click; Page tokens do not expire and a new form needs a new source anyway.
	# RETIRED 2026-08-31 — Task::CRM Task::guards. Both guards were a second reading of the task TYPE, which `activity.api.compute_activity` already enforces on the only writers there are: it refuses a missing required field ("{0} is required.") and an in-person activity with no fix, then `set_or_check_anchor` throws out of range. Bulk complete is `refuse_disabled_bulk_complete`, which carries no switch and gates both bulk doors. Dormant in prod since go-live with every one of these working, which is the proof the form layer owns them.
	Auto(
		key="Access::Desk::sanitize",
		guard=True,
		fires_on="Doc Event",
		trigger_detail="every doctype · validate · access/xss_guard.sanitize_unterminated_tags",
		purpose=(
			"Cleans any saved value that holds an unclosed HTML tag, using the same safety filter the "
			"system already runs. This stops a hidden script in a field from running when a list shows "
			"it. When this is off, values save exactly as the stock system saves them."
		),
		# This toggle governs the sanitiser only. The link-scheme guard is the OTHER write-time browser-safety
		# doc_event in access/ and is deliberately ALWAYS ON — an https-only rule needs no operator opinion and
		# tests/access/test_link_scheme.py locks it against becoming dormant — so it carries no switch of its
		# own and is covered here so the drift lock passes. Same shape and same reason as the always-on SSRF
		# guard carried by Partner::AsyncBulk::jobs.
		# The LMS member-IDOR guard is a third always-on access write-guard with no switch (locked by tests/security/test_lms_member_idor.py), carried here so the drift lock passes — same shape and reason as link_scheme above.
		# The Custom Field fieldname guard is the fourth: a fieldname must be a legal SQL identifier, always on, no switch (locked by tests/access/test_custom_field_guard.py); a before_validate doc_event so it survives ignore_validate.
		# The Insights Team broadcast is the fifth: a team change reaches every worker's grant cache at once, always on, no switch (locked by tests/access/test_insights_team_change_reaches_every_worker.py).
		# The Contact phone gate is the sixth: a new or changed phone is stored E.164 on every path, always on, no switch (locked by tests/helpdesk/test_contact.py).
		# The inbox Create Contact refusal is the seventh: only HD Settings' sender-only tick makes contacts from email, always on, no switch.
		backs=[
			"tatva_connect.access.xss_guard.sanitize_unterminated_tags",
			"tatva_connect.access.link_scheme.guard_link_schemes",
			"tatva_connect.access.lms_member_guard.enforce_member",
			"tatva_connect.access.custom_field_guard.guard_custom_field",
			"tatva_connect.access.insights_team.broadcast_grant_change",
			"tatva_connect.helpdesk.contact.canonical_phones",
			"tatva_connect.helpdesk.contact.refuse_inbox_contacts",
		],
	),
	Auto(
		key="Access::Grain::registry",
		fires_on="Permission",
		trigger_detail="access/entitlement · entitled_grains + grain_entitled source",
		purpose=(
			"Reads each user's business line, group and program from the access granted to them. A lead "
			"can then be filed only under a combination listed in the grain registry. When this is off, "
			"each user's slice comes from their assignment rules, as before."
		),
		# A gate inside access/entitlement (keyed on this row), NOT a doc_event — so backs is empty, like
		# the visibility rows. Drift walks only doc_event/scheduler paths.
		backs=[],
	),
	Auto(
		key="Task::CRM Task::visibility",
		fires_on="Permission",
		trigger_detail="CRM Task · permission_query_conditions + has_permission",
		purpose=(
			"Shows a rep only the tasks they own, the tasks assigned to them, and tasks on leads or deals "
			"they can already see. Operators still see every task. When this is off, every agent sees "
			"every task in the business."
		),
		# Gated by the accessor inside tasks/permissions.py (keyed on this row), NOT a
		# doc_event — so backs is empty, like Telephony/Location. The permission-hook
		# targets must stay OUT of the registry (drift walks only doc_events+scheduler;
		# perm/override targets are deliberately un-backed). See test_drift_ignores_overrides.
		backs=[],
	),
	Auto(
		key="Telephony::CRM Call Log::visibility",
		fires_on="Permission",
		trigger_detail="CRM Call Log · permission_query_conditions + has_permission",
		purpose=(
			"Shows a rep only their own call logs and the calls on leads or deals they can already see. "
			"Operators still see every call. When this is off, every agent sees every call log in the "
			"business."
		),
		# Gated by the shared brain inside access/visibility.py (keyed on this row), NOT a
		# doc_event — so backs is empty, like Task::CRM Task::visibility. The permission-hook
		# targets stay OUT of the registry (drift walks only doc_events + scheduler).
		backs=[],
	),
	Auto(
		key="Note::FCRM Note::visibility",
		fires_on="Permission",
		trigger_detail="FCRM Note · permission_query_conditions + has_permission",
		purpose=(
			"Shows a rep only their own notes and the notes on leads or deals they can already see. "
			"Operators still see every note. When this is off, every agent reads every note the team has "
			"written."
		),
		# Gated by the shared brain inside access/visibility.py (keyed on this row), NOT a
		# doc_event — so backs is empty, like Telephony::CRM Call Log::visibility. The
		# permission-hook targets stay OUT of the registry (drift walks only doc_events + scheduler).
		backs=[],
	),
	Auto(
		key="WhatsApp::WhatsApp Message::visibility",
		fires_on="Permission",
		trigger_detail="WhatsApp Message · permission_query_conditions + has_permission",
		purpose=(
			"Shows a rep only their own WhatsApp messages and the messages on leads or deals they can "
			"already see. Operators still see every chat. When this is off, every agent can read every "
			"patient chat."
		),
		# Gated by the shared brain inside access/visibility.py (keyed on this row), NOT a
		# doc_event — so backs is empty, like Telephony::CRM Call Log::visibility. The
		# permission-hook targets stay OUT of the registry (drift walks only doc_events + scheduler).
		backs=[],
	),
	Auto(
		key="Access::Record::acl",
		guard=True,
		fires_on="Permission",
		trigger_detail="CRM Lead / CRM Deal · permission_query_conditions",
		purpose=(
			"Answers lead and deal lists from a saved index of who may see each record, so a rep's list "
			"loads fast on a large table. It changes no one's access, and any doubt falls back to the "
			"standard rule. When this is off, the index is neither read nor written. An operator rebuilds "
			"the index before turning this on."
		),
		# The handlers that keep the index current, gated by this switch so a dormant index costs no write; arming runs `rebuild` first, and `guard` keeps Quiet imports writing once armed.
		backs=[
			"tatva_connect.access.record_access.on_subject_saved",
			"tatva_connect.access.record_access.on_assignment",
			"tatva_connect.access.record_access.on_assignment_change",
			"tatva_connect.access.record_access.on_subject_deleted",
		],
	),
	Auto(
		key="Contact::Contact::visibility",
		fires_on="Permission",
		trigger_detail="Contact · permission_query_conditions",
		purpose=(
			"Hides staff from the contact list, so reps see only patients and customers. A contact tied "
			"to a staff login is hidden from everyone but an operator. Customers with a portal login stay "
			"visible. When this is off, every staff member shows up in the contact list."
		),
		# Gated inside access/contact_scope.py, not a doc_event — backs is empty for the reason the four visibility rows above give.
		backs=[],
	),
	# RETIRED 2026-10-03 — the Journey, Signal, Step Log and Version visibility switches, folded into Workflow::CRM Workflow::visibility.
	# RETIRED 2026-09-25 — Workflow::Authoring::surface. The Automation Manager role is the one gate for the Workflows screen, read through the CRM Workflow DocPerm; a second on/off switch on top of it was a second answer to the same question.
	Auto(
		key="Workflow::CRM Workflow::visibility",
		fires_on="Permission",
		trigger_detail="CRM Workflow, Version, Journey, Signal, Step Log · permission_query_conditions + has_permission",
		purpose=(
			"Shows a person only the workflows written for the business lines they work in. A workflow "
			"with no business line stays visible to everyone. Its versions, journeys and step logs follow "
			"the same rule. When this is off, anyone who opens the Workflows list sees every workflow."
		),
		# Gated inside the visibility brain, not a doc_event, so `backs` is empty — the drift lock walks doc_events and scheduler entries only.
		backs=[],
	),
	Auto(
		key="Workflow::Engine::sends",
		fires_on="Provider call",
		trigger_detail="automation/sends gate · Send WhatsApp / Send Email effect verbs",
		purpose=(
			"Lets workflows send their WhatsApp messages and emails for real, through the WhatsApp "
			"account set up for each lead and the standard mailer. When this is off, a workflow still "
			"runs every step. Its log records each send as suppressed, and no message goes out."
		),
		backs=[],
		requires="Workflow::Engine::run",
	),
	Auto(
		key="Workflow::Engine::run",
		fires_on="Doc Event",
		trigger_detail='wildcard "*" · on_update (Created, Updated, a task reaching a terminal status) + on_trash (Deleted) — workflow entry',
		purpose=(
			"Runs workflows. When a lead or other record enters an enabled workflow for its business "
			"line, a journey starts and moves through its steps, waits and branches. A record runs the "
			"same workflow only once. When this is off, no journey starts, no waiting journey wakes, and "
			"no signal is delivered."
		),
		backs=[
			"tatva_connect.workflow_engine.triggers.on_created",
			"tatva_connect.workflow_engine.triggers.on_updated",
			"tatva_connect.workflow_engine.triggers.on_trash",
			"tatva_connect.workflow_engine.triggers.on_task_done",
			# Lead work (routing.lead_work: a task worked, an outbound call): the rep has room again, so that pool's waiting journeys are driven.
			"tatva_connect.lead.routing.on_lead_work",
			# The same engine ENDING a journey: the subject left (deleted, or moved grain), so its journeys end with it.
			"tatva_connect.workflow_engine.triggers.on_lead_deleted",
			"tatva_connect.workflow_engine.triggers.on_lead_grain_changed",
			# The engine's own backstop: every 15 min, book a lost wake drain and re-drive buffered signals.
			"tatva_connect.workflow_engine.wakeups.sweep",
			# Always on, no switch: a master a node names cannot be deleted or renamed while a draft or runnable version names it; carried here so the drift lock passes.
			"tatva_connect.integrity.field_usage.guard_record",
			# Always on, no switch: a lead merge stays in one grain and ends the dropped lead's journeys (locked by tests/merge/test_merge.py).
			"tatva_connect.merge.api.before_rename",
		],
	),
	Auto(
		key="Document::Generation::render",
		fires_on="Provider call",
		trigger_detail="workflow_engine/document_render gate · Generate Document effect verb",
		purpose=(
			"Lets a workflow build a PDF for one patient from a document template, filled with that "
			"patient's details. The file is saved on its own record, and the waiting journey is told it "
			"is ready. When this is off, the workflow takes the step's failed path, records why, and "
			"makes no file."
		),
		backs=[
			"tatva_connect.workflow_engine.document_render.render_document",
			# The other half of the same layer's housekeeping: a deleted lead takes its documents, and their blobs, with it.
			"tatva_connect.tatva_connect.doctype.crm_campaign_document.crm_campaign_document.drop_for_lead",
		],
		requires="Workflow::Engine::run",
	),
	Auto(
		key="Storage::File::privacy",
		guard=True,
		fires_on="Doc Event",
		trigger_detail="File · before_insert (FileOverride class override)",
		purpose=(
			"Makes a file public only when the record it is attached to is a type on the operator's "
			"public list, such as logos. All other files stay private, and that includes every patient "
			"file. When this is off, no attachment is made public, and every stored file stays private."
		),
		# apply_privacy_policy is called by FileOverride.before_insert, an override_doctype_class seam and NOT a doc_event (as a hook it ran after core had written the bytes), so backs is empty — drift walks only doc_event/scheduler paths and this target stays OUT of the registry.
		backs=[],
	),
	# RETIRED 2026-08-10 — Lead::CRM Lead::headline. Leaving the registry is how a switch retires: automation.seed.sync_catalog prunes the CRM Tatva Automation row whose key no longer appears here. The lab section already carries the same five values and every surface reads the latest row through multirow.latest_child_row.
	Auto(
		key="Lead::Enrolment::intake",
		fires_on="Doc Event",
		trigger_detail="ANY per-form intake sink · after_insert (*)",
		purpose=(
			"Turns each submitted intake form into the record it creates. That is a lead with every "
			"answer filled in, or a ticket linked to the person's contact. When this is off, the "
			"submission is stored, but no lead or ticket is built from it."
		),
		# route_submission = the wildcard brain for per-form sinks; bust_intake_doctype_cache = the
		# guard-set cache invalidator (a CRM Intake Form on_update/on_trash doc_event, hence backed
		# here too). Every intake form has its OWN per-form runtime sink — no shared legacy staging.
		backs=[
			"tatva_connect.intake.intake.route_submission",
			"tatva_connect.intake.intake.bust_intake_doctype_cache",
			# CRM Intake Form on_update -> scaffold/sync the per-form DocType + Web Form.
			"tatva_connect.intake.builder.sync_form",
		],
	),
	Auto(
		key="Intake::RateLimit::enforcement",
		fires_on="Provider call",
		trigger_detail="before_request · web-form accept · per-IP + per-phone counters",
		purpose=(
			"Caps how often one device or one phone number can submit an enrolment form, ask if a patient "
			"is already enrolled, or upload files. It also clears out uploaded files that never reached a "
			"lead. When this is off, only the site's built-in limit applies."
		),
		# The before_request throttle needs no backs; the guest-orphan reaper is a scheduler_events job gated by this same flag, so drift requires it listed here.
		backs=["tatva_connect.intake.guards.reap_guest_orphans"],
	),
	Auto(
		key="Storage::File::screening",
		guard=True,
		fires_on="Doc Event",
		trigger_detail="File · before_insert (FileOverride class override)",
		purpose=(
			"Checks each uploaded file before it is saved: its contents must match its type, and a virus "
			"scanner looks for malware. Only channels listed in File Screening Settings are checked, such "
			"as web forms or the partner API. When this is off, files get only the standard type and size "
			"checks."
		),
		# file_screening.screen is called by FileOverride.before_insert, an override_doctype_class seam and NOT a doc_event (as a hook it scanned bytes already on disk), so backs is empty — drift walks only doc_event/scheduler paths and this target stays OUT of the registry.
		backs=[],
		activator="tatva_connect.storage.file_screening.apply_scan_logging",
	),
	Auto(
		key="Partner::RateLimit::enforcement",
		fires_on="Provider call",
		trigger_detail="api/_base · @_api + _run_bulk gate",
		purpose=(
			"Limits how many calls and records each partner can send to the partner API, per minute and "
			"per day. This stops one partner from slowing the CRM for everyone. When this is off, partner "
			"calls are not limited."
		),
		# A gate inside _base (@_api + _run_bulk), NOT a doc_event/scheduler — like
		# Telephony/Location/visibility, so backs is empty (drift walks only doc_events
		# + scheduler paths; this gate's target stays OUT of the registry).
		backs=[],
	),
	Auto(
		key="Partner::AsyncBulk::jobs",
		fires_on="Provider call",
		trigger_detail="api/partner_bulk_job · submit gate; daily 04:23 stranded-job reaper + retention purge",
		purpose=(
			"Lets a partner send a large batch of records as one background job, and lets the Desk lead "
			"import run. Each night it marks stuck jobs as failed and removes old upload files. When this "
			"is off, new jobs are refused, and the regular single and small batch calls still work."
		),
		# The submit gate lives inside api/partner_bulk_job (not a doc_event), so the toggle has no
		# doc_event target; the tier's one doc_event is the always-on SSRF guard on a partner's
		# completion-webhook URL, covered here so the drift lock passes.
		backs=[
			"tatva_connect.api.partner_bulk_job.guard_webhook_url",
			# The tier's own backstop: fail a job whose worker died mid-drain and drop its upload.
			"tatva_connect.api.partner_bulk_worker.reap_stranded_jobs",
			# The tier's own housekeeping: drop a finished job's upload past retention; the job and its results stay.
			"tatva_connect.api.partner_bulk_job.purge_expired_jobs",
		],
	),
	Auto(
		key="Lead::BulkImport::desk",
		fires_on="Doc Event",
		trigger_detail="lead_import/api · validate + import gate; CRM Bulk Job · on_update",
		purpose=(
			"Lets staff load leads from a spreadsheet in the Desk, with each column mapped to a lead "
			"field. Every file runs a trial check first, so refused rows show before anything is saved, "
			"and bulk jobs must also be on. When this is off, the import form cannot check or load a "
			"file."
		),
		# The gate lives inside lead_import/api (_queue), not a doc_event; the surface's one doc_event is
		# the status mirror from a finished job back onto its import, covered here so the drift lock passes.
		backs=["tatva_connect.lead_import.api.follow_job_status"],
		requires="Partner::AsyncBulk::jobs",
	),
	Auto(
		key="Setup::SmartSetup::desk",
		fires_on="Doc Event",
		trigger_detail="smart_setup/api · start gate for Build, Check and Apply",
		purpose=(
			"Moves a task type, web form, API contract or workflow to another site as one file, or copies "
			"one grain's setup onto another. Every import is checked first and then applied whole or not "
			"at all. When this is off, Smart Setup cannot build, check or apply anything."
		),
	),
	Auto(
		key="Observability::Requests::logging",
		fires_on="Provider call",
		trigger_detail="after_request · partner-API + inbound-webhook endpoints",
		purpose=(
			"Records every partner API call, inbound webhook and docs server call in the API request log, "
			"with its status and how long it took. This log feeds the monitoring dashboard. When this is "
			"off, nothing is recorded and the dashboard stays empty."
		),
		backs=["tatva_connect.observability.capture.log_request"],
		activator="tatva_connect.observability.capture.apply_logging",
	),
	# REPURPOSED 2026-10-17 — Notify::Partner::silence (an hourly partner-silence email) became this switch; a batch Webhook on No Rows Found does that job, to Teams.
	Auto(
		key="Notify::Webhook::batches",
		fires_on="Schedule",
		trigger_detail="every 5/10/15 min, hourly, 4-hourly or daily per Webhook · Doc Event scheduled_batch",
		purpose=(
			"Lets a webhook send one message per time window, such as every 10 minutes, instead of one "
			"per saved record. The message lists up to ten matching records and the total, and it can "
			"also fire when no records arrive. When this is off, no batched webhook sends."
		),
		backs=[
			"tatva_connect.observability.batched_webhooks.run_every_5_minutes",
			"tatva_connect.observability.batched_webhooks.run_every_10_minutes",
			"tatva_connect.observability.batched_webhooks.run_every_15_minutes",
			"tatva_connect.observability.batched_webhooks.run_hourly",
			"tatva_connect.observability.batched_webhooks.run_every_4_hours",
			"tatva_connect.observability.batched_webhooks.run_daily",
			# The always-on save guard (a batch webhook must be JSON), covered here so the drift lock passes.
			"tatva_connect.observability.batched_webhooks.validate_batch",
		],
		activator="tatva_connect.observability.batched_webhooks.apply_batches",
	),
	Auto(
		key="Observability::Metrics::rollup",
		fires_on="Schedule",
		trigger_detail="every 6h",
		purpose=(
			"Every six hours, sums the API request log into small totals that the monitoring charts read. "
			"This keeps the charts fast as the log grows. When this is off, the charts show whatever the "
			"last run left behind."
		),
		backs=["tatva_connect.observability.rollup.run"],
		requires="Observability::Requests::logging",
		activator="tatva_connect.observability.rollup.apply_rollup",
	),
	Auto(
		key="Learning::Course Lesson::office-embeds",
		fires_on="Provider call",
		trigger_detail="lms.lms.utils.get_lesson · read time",
		purpose=(
			"Shows a Word, Excel or PowerPoint file shared from SharePoint or OneDrive as a read-only "
			"panel inside a training lesson. The file must be shared so anyone with the link can open it. "
			"When this is off, learners see the plain link."
		),
		# An OVERRIDE target, not a doc_event or a scheduled job, so `backs` is empty — the shape the AI Voice and Transcription channel rows already have.
		backs=[],
	),
	Auto(
		key="Search::Index::indexing",
		fires_on="Doc Event",
		trigger_detail="frappe sqlite_search · index-on-save + query gate",
		purpose=(
			"Turns on the global search, so staff can find a lead or file by patient name, phone number "
			"or lead ID. The index is built once in the background and updates as records are saved, and "
			"each person sees only what they may open. When this is off, search returns nothing."
		),
		# A gate read by is_search_enabled; per-save indexing rides frappe's own sqlite_search doc_events. Ours restamps a lead's File rows when its name changes; the activator builds the index once on enable.
		activator="tatva_connect.search.activation.apply",
		backs=[
			"tatva_connect.search.index.reindex_on_lead_context_change",
			# Hourly: drops an index that can no longer be READ, the one damaged state frappe's own 3-hourly check cannot see — it asks whether the file exists, not whether it opens. Gated here because a disabled feature has no index to keep healthy.
			"tatva_connect.search.index.sweep_index_health",
		],
	),
	Auto(
		key="Activity::Timeline::indexing",
		fires_on="Doc Event",
		trigger_detail="activity/timeline · pointer written on save, dropped on delete",
		purpose=(
			"Makes a lead's Activity tab fast by keeping a list of its calls, notes, tasks, files, "
			"comments, emails and WhatsApp messages. A lead with years of history opens as fast as a new "
			"one. When this is off, the tab gathers every entry each time it opens, which is slower."
		),
		activator="tatva_connect.activity.timeline.activate",
		backs=[
			"tatva_connect.activity.timeline.index_event",
			"tatva_connect.activity.timeline.drop_event",
		],
	),
	Auto(
		key="Search::Query::vocabulary",
		fires_on="Provider call",
		trigger_detail="search/api · search gate · typed words split into filters + text",
		purpose=(
			"Reads known words in a search, such as a stage, vertical, group or program, and uses them as "
			"filters. Only the other words are matched as text, so 'onco kavita' finds Onco patients "
			"named Kavita. When this is off, every typed word is matched as text."
		),
		requires="Search::Index::indexing",
	),
	Auto(
		key="MCP::Docs::server",
		fires_on="Provider call",
		trigger_detail="mcp/server · endpoint gate",
		purpose=(
			"Lets staff connect their own AI assistant to this site to ask how to do a task. It reads the "
			"handbook, form fields, Desk pages and the rows of only the setup tables listed in Platform "
			"Settings → Access, under the person's own access, and changes nothing. When this is off, the "
			"server refuses every call."
		),
		# A gate inside mcp/server (the endpoint preamble), NOT a doc_event/scheduler — like
		# Partner::RateLimit::enforcement, so backs stays empty and drift never looks for it.
		backs=[],
	),
]

# Import-time graph gate: a `requires` naming a dead key, or a cycle, cannot reach the catalog.
_PARENT_OF = assert_valid_graph(AUTOMATIONS)
_GUARDS = frozenset(auto.key for auto in AUTOMATIONS if auto.guard)


def parent_of(key):
	"""The ONE parent lookup — the key this row requires, or `""` when it stands alone."""
	return _PARENT_OF.get(key, "")


def area_of(key):
	"""The ONE area derivation — segment 1 of the key, as this module's docstring declares."""
	return (key or "").split("::")[0]


def is_guard(key):
	"""Whether this toggle is a data check — declared on its row, read here, never listed twice."""
	return key in _GUARDS


def activator_for(key):
	for auto in AUTOMATIONS:
		if auto.key == key:
			return auto.activator
	return ""
