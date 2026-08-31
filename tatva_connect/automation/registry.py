"""The ONE catalog — every automation in `tatva_connect` is one row here.

Every automation is an operator toggle, gated via `is_enabled(key)`: ON -> the
tatva_connect hook rides and enforces; OFF -> it's dormant and native frappe/crm
behaviour stands. No locked/always-on class — the operator owns every `enabled`.

`fires_on`: Doc Event · Schedule · Provider call · Permission.
`Area` is derived — segment 1 of the composite key (`key.split("::")[0]`).
`purpose`: one paragraph in the same shape for every row — what happens while the row
is on, then what stands while it is off — followed by one `Example:` line naming the
moment a user meets it. Third person, no "you". Seeded into `description` on migrate.
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

	# Import-time shape gate: a malformed key cannot reach the catalog, so drift fails the build.
	def __post_init__(self):
		assert_valid_key(self.key)
		if self.requires:
			assert_valid_key(self.requires)


AUTOMATIONS = [
	Auto(
		key="Workflow::Cohort::drain",
		fires_on="Schedule",
		trigger_detail="every 15m · starts a drain for each workflow whose cohort is due",
		purpose=(
			"A workflow can take a COHORT on a schedule instead of waiting for a save: every lead matching "
			"its criteria and grain is given its own ordinary journey down the same graph, walked by one job in "
			"committed chunks so a cohort of thousands is one piece of work rather than thousands. Off, a "
			"workflow set to repeat simply never comes due and no journey is born from a clock.\n"
			"Example: on the first of the month every enrolled patient on a program is started down the "
			"renewal journey."
		),
		backs=["tatva_connect.workflow_engine.drain.sweep"],
		requires="Workflow::Engine::run",
	),
	Auto(
		key="AI Voice::Channel::calls",
		fires_on="Provider call",
		trigger_detail="automation/sends.send_voice gate · webhooks/spine kill-switch · voice ingress",
		purpose=(
			"AI voice is opened up on the lead: the outbound calls an automation places through the voice "
			"account named on the node, and the outcome of each call coming back from the provider. Off, "
			"BOTH directions are inert — a routed lead takes the call node's failed edge and nothing is "
			"dialled, and an outcome that arrives is recorded against the delivery log rather than acted "
			"on, so nothing is lost while it is switched off.\n"
			"Example: an enrolled patient is called by the welcome agent, and whether they answered "
			"routes the journey's next step."
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
			"A call whose outcome webhook never arrived is chased up: the provider is asked directly what "
			"became of it, and a journey waiting on that call is moved on. The webhook is the fast path "
			"and it is not a guarantee — a dropped callback otherwise leaves the patient's journey stopped "
			"with nothing to show for it. Off, only the webhook can move such a journey.\n"
			"Example: a call that completed during a deploy, whose callback was lost, still advances the "
			"journey at the next sweep."
		),
		backs=["tatva_connect.voice.reconcile.sweep"],
		requires="AI Voice::Channel::calls",
	),
	Auto(
		key="Storage::Recording::retry",
		fires_on="Schedule",
		trigger_detail="every 15m · retries call recordings whose download failed, with backoff",
		purpose=(
			"A call recording the CRM could not collect first time is fetched again: the producer publishes "
			"the audio, and a timeout or a bad minute at their end otherwise leaves that call with a "
			"transcript and no sound for ever. Each call is retried a few times over a day and then left "
			"alone with the reason on its record. Off, a recording that failed to download stays missing "
			"and the call simply shows no audio.\n"
			"Example: a patient's call is recorded during a network blip, and the audio is on the call an "
			"hour later without anyone asking for it."
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
			"A transcription service is allowed to post the text of a recorded call back into the CRM, "
			"against the call it belongs to and in the same shape every other transcript takes. Off, a "
			"posted transcript is recorded against the delivery log rather than stored, so nothing is lost "
			"while it is switched off and it can be replayed once it is on.\n"
			"Example: a call recorded today is transcribed overnight and the text is on the call in the "
			"morning."
		),
		backs=[],
	),
	Auto(
		key="Notifications::Tray::retention",
		fires_on="Schedule",
		trigger_detail="operator-armed · daily purge of read tray rows",
		purpose=(
			"Read notifications older than the retention floor are deleted from the bell tray, which is "
			"otherwise append-only: a row is written when a rep is told something and never removed, so "
			"the table and every tray read grow without bound. An UNREAD row is never purged however old "
			"— it is an outstanding work item, and age is not consent. The row is dormant and unscheduled "
			"by default.\n"
			"Example: an assignment a rep read four months ago stops being fetched every time they open "
			"the bell."
		),
		backs=["tatva_connect.notifications.retention.purge_read_notifications"],
	),
	Auto(
		key="WhatsApp::Channel::messaging",
		fires_on="Provider call",
		trigger_detail="whatsapp/api gate · WhatsApp Message · before_save",
		purpose=(
			"WhatsApp is opened up on the lead: the templates and messages sent out of the CRM, and "
			"the patient replies that come back, both ride the WhatsApp account routed to that lead's "
			"grain, through whichever provider that account names. Off, the WhatsApp tab and its send "
			"actions are inert — nothing leaves and nothing lands.\n"
			"Example: an approved template is sent to a lead, and the patient's reply arrives back "
			"on that same lead's WhatsApp tab."
		),
		backs=["tatva_connect.whatsapp.webhook.pin_inbound_reference"],
	),
	Auto(
		key="WhatsApp::Channel::templates",
		fires_on="Schedule",
		trigger_detail="every 6h",
		purpose=(
			"The approved-template list is refreshed from the provider every six hours, so the picker a rep "
			"sends from is always the current one and nobody syncs it by hand. Off, the list is "
			"frozen at whatever the last pull left behind.\n"
			"Example: a reminder template approved by marketing on the provider appears in the Send Template "
			"picker within six hours."
		),
		backs=["tatva_connect.whatsapp.templates_sync.scheduled_sync_all"],
		requires="WhatsApp::Channel::messaging",
	),
	Auto(
		key="WhatsApp::Channel::reconcile",
		fires_on="Schedule",
		trigger_detail="operator-armed · getMessages history pull",
		purpose=(
			"A lead's WhatsApp tab is topped up from the provider's message history: the full two-way "
			"thread is pulled, including replies typed straight into the provider's portal, and any "
			"message the live webhook missed is inserted, de-duplicated by the provider's message id. "
			"The row is dormant "
			"and unscheduled by default; a cron is armed by the operator when a gap needs filling.\n"
			"Example: messages an agent answered inside the provider's portal during a webhook outage are "
			"brought onto the lead's WhatsApp tab."
		),
		backs=["tatva_connect.whatsapp.backfill.scheduled_backfill"],
		requires="WhatsApp::Channel::messaging",
	),
	Auto(
		key="WhatsApp::Channel::recovery",
		fires_on="Provider call",
		trigger_detail="orphan status · one-message conversation read",
		purpose=(
			"A delivery or read receipt that arrives for a message this CRM never stored no longer "
			"vanishes: the one message the receipt names is fetched from the provider's own "
			"conversation, filed on the lead it belongs to with its attachment, and the receipt applied "
			"to it. Only that message is fetched, and a message already held is never fetched twice. "
			"Off, such a receipt is recorded and dropped, exactly as before.\n"
			"Example: a read receipt lands for a template an agent sent from the provider's portal "
			"during a webhook outage, and both the message and its blue tick appear on the lead."
		),
		backs=[],
		requires="WhatsApp::Channel::messaging",
	),
	Auto(
		key="WhatsApp::Channel::media-retry",
		fires_on="Schedule",
		trigger_detail="every 15 min · owed media, backoff over ~1 day",
		purpose=(
			"A photo or document that failed to download when its message arrived is asked for again, "
			"on a widening backoff, and appears on the message once it lands. Only messages the "
			"provider said carried media are retried, each is given a fixed number of attempts, and a "
			"message whose attempts are spent is left alone. Off, a download that failed when the "
			"message arrived is never retried and the message keeps its 'Media unavailable' note.\n"
			"Example: the provider has a bad minute while a patient sends a prescription photo, and the "
			"photo appears on the lead's WhatsApp tab a few minutes later instead of never."
		),
		backs=["tatva_connect.whatsapp.media_retry.sweep"],
		requires="WhatsApp::Channel::messaging",
	),
	Auto(
		key="Telephony::Channel::calls",
		fires_on="Provider call",
		trigger_detail="telephony/api gate",
		purpose=(
			"Click-to-call and call logging are enabled on the telephony line routed to the lead's "
			"grain, through whichever provider that line names. Off, a lead's number cannot be "
			"dialled from the CRM and no call is written back to it.\n"
			"Example: a lead's number is clicked, the provider bridges the call to the rep's phone, "
			"and the call is pulled into that lead's call log."
		),
		backs=[],
	),
	Auto(
		key="Telephony::Channel::reconcile",
		fires_on="Schedule",
		trigger_detail="operator-armed · call records pull",
		purpose=(
			"A lead's call log is topped up from the provider's call records: the calls on the lead's "
			"routed line are pulled and any the live webhook missed are written in, de-duplicated by "
			"the provider's call id. Calls a rep logged by hand are never touched. The row is "
			"dormant and unscheduled by default; a cron is armed by the operator when a gap needs "
			"filling.\n"
			"Example: an afternoon of calls lost to a webhook outage is brought back onto the lead's "
			"call log."
		),
		backs=["tatva_connect.telephony.reconcile.scheduled_reconcile"],
		requires="Telephony::Channel::calls",
	),
	Auto(
		key="Storage::Azure::offload",
		fires_on="Doc Event",
		trigger_detail="File · after_insert + on_trash",
		purpose=(
			"Every uploaded attachment is moved off the app server into Azure Blob storage, and its "
			"blob is removed when the file is deleted, which keeps the CRM disk lean. Off, files "
			"stay on the server exactly as frappe stores them.\n"
			"Example: a prescription photo attached to a lead is held in Azure and served back "
			"through a proxy link rather than kept on the server."
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
			"On a visit type marked location-tracked, the rep's GPS is captured at completion and "
			"checked against the clinic's pinned location, and the result is recorded as a "
			"tamper-proof visit trail. The same switch pins a NEW doctor at the position the rep "
			"created them from, so the very first visit is measured against a real location instead "
			"of setting one. Off, a visit is closed with no location captured and no check made, and "
			"a new doctor is created with no pinned location.\n"
			"Example: a doctor visit is marked done, the rep's coordinates are captured, confirmed "
			"to be within the allowed radius of the clinic, and logged as a Visit Audit entry."
		),
		backs=["tatva_connect.location.api.capture_on_create"],
	),
	Auto(
		key="Location::NearMe::directory",
		fires_on="Provider call",
		trigger_detail="near_me/api gate",
		purpose=(
			"The Near Me page is powered: every doctor lead with a pinned clinic is drawn on a "
			"cross-business-line map and list, so a rep on the road can find and call the doctors "
			"around them. It opens only for a user holding the Field Map User role, and only while "
			"this row is on. Off, the page is not reachable at all.\n"
			"Example: doctors within 15 km of the phone's location are shown on a map, and a card is "
			"tapped to call one or get directions."
		),
		backs=[],
	),
	Auto(
		key="Lead::BulkActions::async",
		fires_on="Provider call",
		trigger_detail="bulk_actions.run_or_queue gate · CRM Lead list-view multi-select",
		purpose=(
			"A list-view bulk action — Assign, Clear Assignment, Bulk Edit, Bulk Delete — on 20 or more "
			"selected leads moves to a background job instead of running inside the browser's request, "
			"and the rep is told when it finishes rather than left staring at a tab that may time out on "
			"a large selection. Off, the action still runs inline, but not exactly as it did before this "
			"seam existed: frappe's own per-action thresholds (Bulk Edit's own 20-row enqueue point, Bulk "
			"Delete's own 10-row enqueue point) are bypassed regardless of the switch, replaced by one "
			"uniform 500-row cap above which the selection is refused outright, with no background "
			"offload while the switch stays off.\n"
			"Example: a rep selects 200 leads and clicks Assign; the browser is freed immediately and a "
			"toast reports 197 assigned, 3 skipped once the job finishes."
		),
		backs=["tatva_connect.bulk_actions.run_or_queue"],
	),
	Auto(
		key="Lead::Assignment::owner",
		fires_on="Doc Event",
		trigger_detail="CRM Lead · after_insert + validate (lead_owner changed)",
		purpose=(
			"A lead naming an owner is put into that person's list: the name on the record becomes a real "
			"assignment, and the lead is shared with them. Off, the owner field still records who owns the "
			"lead and every other route to it is unchanged — a lead that names nobody is unaffected either "
			"way, because frappe's own Assignment Rule is what picks a rep in that case.\n"
			"Example: a manager sets the owner on a walk-in lead, and it appears in that rep's list at once."
		),
		backs=[],  # the fork assigns off a field in crm_lead.py, not a doc_event; gated by lead/assignment.LeadAssignmentGate on the override class
	),
	Auto(
		key="Lead::Assignment::from_rule",
		fires_on="Doc Event",
		trigger_detail="ToDo · after_insert (reference_type = CRM Lead)",
		purpose=(
			"A lead that nobody owns records the person an Assignment Rule just picked as its owner, so "
			"the reports that read the owner column show it. A lead that already names an owner is never "
			"changed. Off, the pick still stands and the rep still holds the lead — it is simply not "
			"written to the owner column, so owner-grouped reports leave those leads blank.\n"
			"Example: a patient submits the enrolment form, the rule hands the lead to a Care Specialist, "
			"and that name appears in the Lead Owner column instead of an empty cell."
		),
		backs=["tatva_connect.lead.assignment.on_assignment_set_owner"],
	),
	Auto(
		key="Task::Assignment::assignee",
		fires_on="Doc Event",
		trigger_detail="CRM Task · after_insert + validate (assigned_to changed)",
		purpose=(
			"A task naming an assignee is put into that person's list: the name on the record becomes a "
			"real assignment. Off, the assignee field still records who holds the task and the list is "
			"reached by other routes. Un-assigning a previous holder is never gated — a task moved to "
			"someone else always releases the person who had it.\n"
			"Example: a rep is named on a follow-up task, and it appears in their list at once."
		),
		backs=[],  # as above: crm_task.py assigns off a field; gated by lead/assignment.TaskAssignmentGate
	),
	# RETIRED 2026-08-31 — Task::Assignment::followup. Predated the workflow engine's own Create Task node (2026-08-10) by two months, which does the same job authored per program instead of one hardcoded rule; the go-live checklist always listed it under switches to confirm OFF, never one to turn on.
	Auto(
		key="Task::Review::mirror",
		fires_on="Doc Event",
		trigger_detail="CRM Task · on_update",
		purpose=(
			"A Document Review task's verdict is copied onto the document it reviewed, so the file "
			"itself carries an Approved or Rejected badge on the Attachments tab. Off, the verdict "
			"lives only on the review task and the file shows nothing.\n"
			"Example: a patient's uploaded prescription is approved on its review task, and the file "
			"on the lead's Attachments tab shows an Approved badge."
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
			"Attachments stranded by sent or abandoned draft emails are cleared each night, so "
			"orphaned files do not pile up; a blob shared with the sent copy is left alive. Off, the "
			"orphans are kept indefinitely.\n"
			"Example: a file attached to an email draft that was then discarded is removed at 02:30."
		),
		backs=["tatva_connect.api.email.purge_draft_attachments"],
	),
	Auto(
		key="Partner::Idempotency::cleanup",
		fires_on="Schedule",
		trigger_detail="daily 04:00",
		purpose=(
			"Partner-API idempotency records — the opt-in write-dedup store — are dropped each night "
			"once past the retention window, so they do not accumulate. Off, the keys are kept "
			"indefinitely.\n"
			"Example: a partner's retry-safety keys from the previous day are cleared at 04:00."
		),
		backs=["tatva_connect.api._base.purge_idempotency_keys"],
	),
	Auto(
		key="Notify::Lead::assigned",
		fires_on="Doc Event",
		trigger_detail="ToDo · after_insert",
		purpose=(
			"The rep a lead is assigned to is told about it — in-app while they are at their desk, a "
			"mobile push when they are away — and each rep opts in per notification. Off, the "
			"assignment is silent and the lead is found only by looking.\n"
			"Example: a manager assigns a lead, and the rep receives a 'New lead assigned' "
			"notification."
		),
		backs=[
			"tatva_connect.notifications.events.on_lead_assigned",
		],
	),
	Auto(
		key="Notify::WhatsApp::received",
		fires_on="Doc Event",
		trigger_detail="WhatsApp Message · after_insert",
		purpose=(
			"The rep a lead is assigned to is told when the patient replies on WhatsApp — in-app "
			"while they are at their desk, a mobile push when they are away — and each rep opts in "
			"per notification. Off, the reply waits silently on the tab.\n"
			"Example: a patient answers a template message, and the rep hears about it rather than "
			"finding it on their next visit to the tab."
		),
		backs=[
			"tatva_connect.notifications.events.on_whatsapp_received",
		],
	),
	Auto(
		key="Notify::Telephony::missed",
		fires_on="Doc Event",
		trigger_detail="CRM Call Log · on_update (status -> No Answer, inbound)",
		purpose=(
			"The rep a lead is assigned to is told when the patient's call went unanswered. It is "
			"always pushed, since a missed call is only worth knowing about before the patient gives "
			"up; each rep opts in per notification. Off, the missed call is found only by reading the "
			"call log.\n"
			"Example: a patient rings the program's number and nobody picks up, and the rep sees it "
			"at once."
		),
		backs=[
			"tatva_connect.notifications.events.on_call_missed",
		],
	),
	Auto(
		key="Notify::Task::due-soon",
		fires_on="Schedule",
		trigger_detail="every 5 min · tasks falling due inside the operator's lead time",
		purpose=(
			"A rep is warned that a task assigned to them is about to fall due. The lead time is the "
			"operator's, set in CRM Notification Settings, and each task is warned about once per due "
			"date, so a rescheduled task warns again. Off, nothing is sent ahead of the due time.\n"
			"Example: a call is due in an hour, and the rep is reminded while there is still time to "
			"make it."
		),
		requires="Notify::Task::assigned",
		backs=[
			"tatva_connect.notifications.events.sweep_due_soon",
		],
	),
	Auto(
		key="Notify::Task::overdue",
		fires_on="Schedule",
		trigger_detail="every 5 min · tasks past their due date and not done",
		purpose=(
			"A rep is told when a task assigned to them has passed its due date and is still not "
			"done. Each task is told about once per due date, so a rescheduled task can tell again "
			"and no task nags on every sweep. Off, an overdue task passes unremarked.\n"
			"Example: yesterday's follow-up call was never made, and the rep is told rather than the "
			"lead going cold."
		),
		requires="Notify::Task::assigned",
		backs=[
			"tatva_connect.notifications.events.sweep_overdue",
		],
	),
	Auto(
		key="Notify::Lead::stage-changed",
		fires_on="Doc Event",
		trigger_detail="CRM Lead · on_update (custom_substage changed)",
		purpose=(
			"The rep a lead is assigned to is told when its stage is moved, whoever moved it — a "
			"manager, an automation rule, or an integration — and each rep opts in per notification. "
			"Off, the lead changes hands quietly.\n"
			"Example: a manager moves a lead to Consent Pending, and the rep is told the ball is in "
			"their court."
		),
		backs=[
			"tatva_connect.notifications.events.on_lead_stage_changed",
		],
	),
	Auto(
		key="Notify::Task::assigned",
		fires_on="Doc Event",
		trigger_detail="CRM Task · after_insert",
		purpose=(
			"The rep a task is assigned to is told about it — in-app while they are at their desk, a "
			"mobile push when they are away — and each rep opts in per notification. Off, the "
			"assignment is silent and the task is found only by looking.\n"
			"Example: a manager assigns a task, and the rep receives a 'New task assigned' "
			"notification."
		),
		backs=[
			"tatva_connect.notifications.events.on_task_created",
		],
	),
	Auto(
		key="Lead::CRM Lead::dedup",
		fires_on="Doc Event",
		trigger_detail="CRM Lead · before_validate + validate",
		purpose=(
			"Each lead's phone and routing fields are normalised, and a second lead for a patient "
			"already carried in the same product line is refused at save. Off, the duplicate is "
			"accepted and two records for one patient run in parallel.\n"
			"Example: a partner re-sends a patient who is already enrolled, the number is recognised, "
			"and the duplicate lead is not created."
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
		fires_on="Doc Event",
		trigger_detail="CRM Lead · before_validate",
		purpose=(
			"Each new lead is filed under its creator's entitled grain — product line, group, "
			"program — rather than the creator being asked to pick one: a single-grain user's grain "
			"is applied for them, a manager's choice is validated against their entitlement, and a "
			"grain that is missing or out of scope is rejected. Off, the grain is whatever is typed, "
			"unchecked.\n"
			"Example: a rep who works one program creates a lead without ever seeing a grain field; "
			"it is filed under their program, and one outside it cannot be filed at all."
		),
		backs=["tatva_connect.lead.leads.stamp_entitled_grain"],
	),
	Auto(
		key="Lead::CRM Lead::stage",
		fires_on="Doc Event",
		trigger_detail="CRM Lead · validate",
		purpose=(
			"The program-specific lead lifecycle is enforced: only the stages that belong to that "
			"lead's program can be set, and the high-level stage is kept in step with them. Off, any "
			"stage can be set on any lead.\n"
			"Example: a stage that does not belong to the lead's program is chosen, and the save is "
			"blocked with a plain message."
		),
		backs=["tatva_connect.lead.leads.validate_stage"],
	),
	Auto(
		key="Deal::CRM Deal::guards",
		fires_on="Doc Event",
		trigger_detail="CRM Deal · before_validate + validate",
		purpose=(
			"A deal is anchored to the lead it came from: it is filed under that lead's product line, "
			"a product line that has not been armed for deals refuses to carry one, and a second deal "
			"on the same customer is refused at save. Off, a deal can be created loose, on any product "
			"line, as many times as anyone likes.\n"
			"A deal may also only be opened from the stage its programme has declared to be the moment "
			"the lead bought, and each sale line's renewal date is worked out from the plan's duration "
			"rather than typed.\n"
			"Example: a rep converts a patient who already has a deal, and the duplicate is refused."
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
		key="Lead::Facebook::form-refresh",
		fires_on="Schedule",
		trigger_detail="daily 01:00",
		purpose=(
			"Every Facebook Page and lead form is re-read from Facebook each night, and each Page's own "
			"access token is renewed in the same pass. A form returned for the first time is added with "
			"its questions; a form already held has its stored questions replaced by the ones Facebook "
			"returns for it. Off, nothing is re-read: the forms and questions stay as the last refresh "
			"left them, a form published since is not listed, and an answer to a question that is not "
			"stored has no mapping to land on.\n"
			"Example: a form is published in the afternoon; by the next morning it is listed with its "
			"questions, each waiting to be pointed at a screening concept."
		),
		backs=["tatva_connect.lead_sync.discovery.refresh_all_sources"],
	),
	# RETIRED 2026-08-31 — Task::CRM Task::guards. Both guards were a second reading of the task TYPE, which `activity.api.compute_activity` already enforces on the only writers there are: it refuses a missing required field ("{0} is required.") and an in-person activity with no fix, then `set_or_check_anchor` throws out of range. Bulk complete is `refuse_disabled_bulk_complete`, which carries no switch and gates both bulk doors. Dormant in prod since go-live with every one of these working, which is the proof the form layer owns them.
	Auto(
		key="Access::Desk::sanitize",
		fires_on="Doc Event",
		trigger_detail="every doctype · validate · access/xss_guard.sanitize_unterminated_tags",
		purpose=(
			"A tag that is never closed — `<iframe src=…` with no `>` — slips past frappe's own write-time "
			"filter, because the filter first asks a strict parser whether the value looks like HTML and a "
			"strict parser says no. A browser is lenient, emits the unclosed tag at end of input, and runs "
			"it. On, every saved value is put back through frappe's OWN sanitiser with that one shortcut "
			"disabled, so nothing new decides what is safe. Off, which is how it ships, a value stores "
			"exactly as frappe stores it today.\n"
			"Example: a task title typed as an unterminated iframe is stored inert instead of executing "
			"when the desk list draws it."
		),
		# This toggle governs the sanitiser only. The link-scheme guard is the OTHER write-time browser-safety
		# doc_event in access/ and is deliberately ALWAYS ON — an https-only rule needs no operator opinion and
		# tests/access/test_link_scheme.py locks it against becoming dormant — so it carries no switch of its
		# own and is covered here so the drift lock passes. Same shape and same reason as the always-on SSRF
		# guard carried by Partner::AsyncBulk::jobs.
		# The LMS member-IDOR guard is a third always-on access write-guard with no switch (locked by tests/security/test_lms_member_idor.py), carried here so the drift lock passes — same shape and reason as link_scheme above.
		# The Custom Field fieldname guard is the fourth: a fieldname must be a legal SQL identifier, always on, no switch (locked by tests/access/test_custom_field_guard.py); a before_validate doc_event so it survives ignore_validate.
		backs=[
			"tatva_connect.access.xss_guard.sanitize_unterminated_tags",
			"tatva_connect.access.link_scheme.guard_link_schemes",
			"tatva_connect.access.lms_member_guard.enforce_member",
			"tatva_connect.access.custom_field_guard.guard_custom_field",
		],
	),
	Auto(
		key="Access::Grain::registry",
		fires_on="Permission",
		trigger_detail="access/entitlement · entitled_grains + grain_entitled source",
		purpose=(
			"A user's business slice — product line, group, program — is read from the permissions they "
			"already hold, and a lead may only be filed under a combination the operator has declared in "
			"the grain registry. This reaches the people the old source never did: a manager, and a rep "
			"entitled to a whole group, both resolve to their true slice instead of to nothing. Off, "
			"which is how it ships, the slice is read from Assignment Rule membership exactly as before "
			"and the registry is not consulted.\n"
			"Example: a rep who covers all of Anaya opens a patient enrolled in any of its programs and "
			"sees that patient's full record, where before the tab came up empty."
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
			"CRM Tasks are scoped to the people who should see them: a task is visible to a rep only "
			"when it is theirs, assigned to them, or hangs off a Lead or Deal already visible to "
			"them. Stock crm scopes Leads and Deals but never Tasks, so off, every agent sees every "
			"task in the business.\n"
			"Example: the Tasks list shows a rep only the tasks on their own leads, not the whole "
			"team's."
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
			"Call logs are scoped to the people who should see them: a call is visible to a rep only "
			"when it is theirs, or hangs off a Lead or Deal already visible to them. Stock crm scopes "
			"Leads and Deals but never call logs, so off, every agent sees every call in the "
			"business.\n"
			"Example: a lead's Calls tab shows a rep only the calls on their own leads, not the whole "
			"team's."
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
			"Notes are scoped to the people who should see them: a note is visible to a rep only when "
			"it is theirs, or hangs off a Lead or Deal already visible to them. Stock crm scopes "
			"Leads and Deals but never notes, so off, every agent reads every note the team has "
			"written.\n"
			"Example: a lead's Notes tab shows a rep only the notes on their own leads, not the whole "
			"team's."
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
			"WhatsApp messages are scoped to the people who should see them: a message is visible to "
			"a rep only when it is theirs, or hangs off a Lead or Deal already visible to them. Stock "
			"crm scopes Leads and Deals but never WhatsApp threads, so off, every agent reads every "
			"patient conversation.\n"
			"Example: a lead's WhatsApp tab shows a rep only the messages on their own leads, not the "
			"whole team's."
		),
		# Gated by the shared brain inside access/visibility.py (keyed on this row), NOT a
		# doc_event — so backs is empty, like Telephony::CRM Call Log::visibility. The
		# permission-hook targets stay OUT of the registry (drift walks only doc_events + scheduler).
		backs=[],
	),
	Auto(
		key="Access::Record::acl",
		fires_on="Permission",
		trigger_detail="CRM Lead / CRM Deal · permission_query_conditions",
		purpose=(
			"Lead and deal lists answer from a written-down index of who may see what, instead of "
			"working it out row by row. Off, which is how it ships, crm's own rule runs unchanged: "
			"correct, and on 172,923 leads it reads every one of them to return a rep's twenty. On, "
			"the same rule is answered from CRM Record Access, so a read costs what the rep can see "
			"rather than what the table holds. It changes NO ONE's access — the index is rebuilt from "
			"crm's own two grants, an operator is never restricted by it, and any doubt falls back to "
			"crm. Arm it only once record_access_audit reports zero divergence on this site.\n"
			"Example: a rep who owns one lead opens their list and the server reads one row, not 169,733."
		),
		# The three handlers that keep the index current — on_assignment is wired to ToDo's after_delete
		# and not on_trash, because on_trash still sees the row it is revoking. They are NOT gated by this switch and must not
		# be: a permission index that only updates while armed is stale the moment it is armed. The switch
		# gates the READ (record_access.condition); these keep the table honest either way.
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
			"The contact list shows customers rather than colleagues: frappe makes one contact for "
			"every login it creates, so the team's own names sit in the list beside the patients. On, "
			"a contact whose login is a staff login is hidden from everyone but an operator; a "
			"customer holding a portal login is untouched, and so is a customer with no login at "
			"all. Off, every rep reads the whole team's names as though they were customers, which "
			"is how stock crm and helpdesk ship.\n"
			"Example: a rep searching the contacts for a patient stops finding their own manager."
		),
		# Gated inside access/contact_scope.py, not a doc_event — backs is empty for the reason the four visibility rows above give.
		backs=[],
	),
	Auto(
		key="Workflow::Authoring::surface",
		fires_on="Provider call",
		trigger_detail="access/surfaces gate · the Workflows menu item and its direct URL",
		purpose=(
			"The Workflows authoring screen is reachable: a user who is permitted to read workflows "
			"sees the Workflows item in the menu and may open it directly. Permission alone is not "
			"enough — this row is the operator's own switch for whether the screen exists yet, so "
			"authoring can be held back until the programme's journeys are ready to be written. Off, "
			"which is how it ships, the menu item is absent and the address is refused. This decides "
			"whether the SCREEN appears and changes nothing about which workflows a person may see — "
			"that stays Workflow::CRM Workflow::visibility.\n"
			"Example: a manager who may read workflows opens the CRM and finds no Workflows item until "
			"the operator turns this on for go-live."
		),
		# Gated inside the surface brain, not a doc_event, so `backs` is empty — the drift lock walks doc_events and scheduler entries only.
		backs=[],
	),
	Auto(
		key="Workflow::CRM Workflow::visibility",
		fires_on="Permission",
		trigger_detail="CRM Workflow · permission_query_conditions + has_permission",
		purpose=(
			"Workflows are scoped to the business line they are written for: a workflow is listed for a person "
			"only when the vertical, group and program it declares overlap what that person is entitled to, and "
			"a workflow declaring no line at all is shown to everyone. A workflow names the fields it reads and "
			"the messages it sends, so off, anyone who can open the Workflows list reads how every other "
			"business line runs. This scopes who may SEE a workflow and changes nothing about which patients it "
			"acts on — that stays the workflow's own declared line and its criteria.\n"
			"Example: a rep on one programme opens Workflows and sees the journeys written for their own "
			"programme, not the whole business's."
		),
		# Same as its three siblings below: gated inside the visibility brain, not a doc_event, so `backs`
		# is empty — the drift lock walks doc_events and scheduler entries only.
		backs=[],
	),
	Auto(
		key="Workflow::CRM Workflow Journey::visibility",
		fires_on="Permission",
		trigger_detail="CRM Workflow Journey · permission_query_conditions + has_permission",
		purpose=(
			"Workflow journeys are scoped to the people who should see them: a journey is visible to a rep only "
			"when it is theirs, or it is about a Lead or Deal already visible to them. A journey carries its "
			"lead's field values in its saved state, so off, anyone who can open the journey list reads "
			"every other grain's lead data.\n"
			"Example: the Workflow Journeys list shows a manager only the journeys on their own leads, not the "
			"whole business's."
		),
		# Gated by the shared brain inside access/visibility.py (keyed on this row), NOT a
		# doc_event — so backs is empty, like Telephony::CRM Call Log::visibility. The
		# permission-hook targets stay OUT of the registry (drift walks only doc_events + scheduler).
		backs=[],
	),
	Auto(
		key="Workflow::CRM Workflow Signal::visibility",
		fires_on="Permission",
		trigger_detail="CRM Workflow Signal · permission_query_conditions + has_permission",
		purpose=(
			"Workflow events are scoped to the people who should see them: a signal is visible to a rep "
			"only when it is theirs, or it is addressed to a Lead or Deal already visible to them. An "
			"event carries the payload that woke a journey, so off, anyone who can open the event list reads "
			"every other grain's lead data.\n"
			"Example: the Workflow Events inbox shows a manager only the signals on their own leads."
		),
		# Gated by the shared brain inside access/visibility.py (keyed on this row), NOT a
		# doc_event — so backs is empty, like Telephony::CRM Call Log::visibility. The
		# permission-hook targets stay OUT of the registry (drift walks only doc_events + scheduler).
		backs=[],
	),
	Auto(
		key="Workflow::CRM Workflow Step Log::visibility",
		fires_on="Permission",
		trigger_detail="CRM Workflow Step Log · permission_query_conditions + has_permission",
		purpose=(
			"Workflow step logs are scoped to the people who should see them: a step is visible to a rep "
			"only when the journey it belongs to is. A step's detail quotes the values the node acted on, so "
			"off, anyone who can open the step list reads every other grain's lead data.\n"
			"Example: a lead's workflow history shows a rep the steps of their own leads' journeys only."
		),
		# Gated by the shared brain inside access/visibility.py (keyed on this row), NOT a
		# doc_event — so backs is empty, like Telephony::CRM Call Log::visibility. The
		# permission-hook targets stay OUT of the registry (drift walks only doc_events + scheduler).
		backs=[],
	),
	Auto(
		key="Workflow::Engine::sends",
		fires_on="Provider call",
		trigger_detail="automation/sends gate · Send WhatsApp / Send Email effect verbs",
		purpose=(
			"The send gate for the engine's Send WhatsApp and Send Email effects. Off, which is how it "
			"ships, a workflow carrying a Send action still fires end to end and the Journey Log records the "
			"intent, but no template and no email ever leaves the building. On, those same actions "
			"send for real, through the existing grain-routed WhatsApp account and frappe's own mailer — "
			"never a second transport.\n"
			"Example: a 'Welcome' workflow is built and tested with the gate off, the Journey Log reading "
			"'suppressed: sends dormant', and the same workflow starts sending the real message the day "
			"it is switched on at go-live."
		),
		backs=[],
		requires="Workflow::Engine::run",
	),
	Auto(
		key="Workflow::Engine::run",
		fires_on="Doc Event",
		trigger_detail='wildcard "*" · validate (guard lane) + after_insert (Created) + on_update (Updated) + on_trash (Deleted) — workflow entry',
		purpose=(
			"The workflow engine itself: a subject entering an enabled workflow whose grain matches "
			"starts one durable journey that walks a graph of steps, branches, and waits, parking on "
			"timers and external signals for as long as it needs. One journey runs per subject, "
			"guarded by a database unique key so a subject can never start the same workflow twice, and "
			"an external event is delivered by dropping a row in a durable inbox that the matching wait "
			"consumes — so an early, duplicate, or out-of-order signal is handled by construction rather "
			"than lost. Off, which is how it ships, no journey starts and no signal is delivered.\n"
			"Example: a lead is created, a welcome step fires, the journey waits for a document upload, "
			"and the upload's signal resumes it weeks later exactly where it parked."
		),
		backs=[
			"tatva_connect.workflow_engine.triggers.on_created",
			"tatva_connect.workflow_engine.triggers.on_updated",
			"tatva_connect.workflow_engine.triggers.on_trash",
			"tatva_connect.workflow_engine.triggers.on_task_done",
			# The same engine ENDING a journey: the subject left (deleted, or moved grain), so its journeys end with it.
			"tatva_connect.workflow_engine.triggers.on_lead_deleted",
			"tatva_connect.workflow_engine.triggers.on_lead_grain_changed",
		],
	),
	Auto(
		key="Workflow::Engine::sweep",
		fires_on="Schedule",
		trigger_detail="every 15 min · timer wake + reconciler re-drive",
		purpose=(
			"The scheduled heartbeat behind the workflow engine's waits: every 15 minutes it wakes each "
			"journey whose timer has elapsed and re-drives any journey whose awaited signal is already "
			"waiting in the inbox but whose wake was lost, so the durable state is always the source of "
			"truth and no lost job can strand a journey. It is gated on its own switch as well as the "
			"engine's, so the sweep can be paused without taking the engine down. Off, timers and "
			"buffered signals simply wait.\n"
			"Example: a journey parked on a two-day timer is resumed by the first sweep after the two "
			"days elapse, even if the original wake job never ran."
		),
		backs=[
			"tatva_connect.workflow_engine.wakeups.sweep",
		],
		requires="Workflow::Engine::run",
	),
	Auto(
		key="Document::Generation::render",
		fires_on="Provider call",
		trigger_detail="workflow_engine/document_render gate · Generate Document effect verb",
		purpose=(
			"A workflow is allowed to produce a document for one patient: the template the node names is "
			"filled with that patient's own values, rendered to a PDF, and filed on a record of its own, "
			"and the journey waiting on it is then told the document is ready. It is filed apart from the "
			"lead deliberately, so a leaflet meant for a patient's phone can be published without "
			"publishing anything else attached to them. Off, which is how it ships, a workflow carrying "
			"the node still runs end to end and takes the node's failed branch with the reason recorded on "
			"the step, so nothing is rendered and no file is created.\n"
			"Example: a patient's own care plan is generated after their assessment call and reaches them "
			"as an attachment, instead of a rep writing one by hand."
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
		fires_on="Doc Event",
		trigger_detail="File · before_insert (FileOverride class override)",
		purpose=(
			"Every attachment's privacy is decided at upload: a patient's file is held private, and "
			"only the doctypes the operator has whitelisted — logos and the like — are made public. "
			"Off, privacy is left to whatever frappe defaults to.\n"
			"Example: a patient's lab report is uploaded and marked private, so only an authorised "
			"user can open it."
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
			"A submitted enrolment or intake form is turned into a fully populated lead: every answer "
			"is mapped to its field and any master it needs is created on the way. Each intake form "
			"owns a runtime submission DocType, and a single wildcard after_insert routes all of them "
			"through the same lead-create brain, a cheap cached guard ignoring every doctype that is "
			"not an intake sink. Off, the submission is stored but no lead is built from it.\n"
			"Example: a patient submits the enrolment web form, and a lead appears with their details, "
			"their program and their attachments already filled in."
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
			"Enrolment-form submissions are capped per IP and per phone, above frappe's native ten a "
			"minute per IP, so a bot or a stuck client cannot flood the form; the caps themselves are "
			"tunable in CRM Intake Settings. Off, the native per-IP limit alone applies, which is "
			"today's behaviour.\n"
			"Example: a script POSTing the form in a loop is throttled with a plain 'try again later', "
			"while a patient submitting once is unaffected."
		),
		# The before_request throttle needs no backs; the guest-orphan reaper is a scheduler_events job gated by this same flag, so drift requires it listed here.
		backs=["tatva_connect.intake.guards.reap_guest_orphans"],
	),
	Auto(
		key="Storage::File::screening",
		fires_on="Doc Event",
		trigger_detail="File · before_insert (FileOverride class override)",
		purpose=(
			"The master switch for the shared file screener: a file's bytes are sniffed against the "
			"type it claims to be, and the file is scanned by ClamAV, over and above frappe's native "
			"size, extension and unsafe-PDF checks. Only the channels the operator lists in CRM File "
			"Screening Settings → Active Channels (Intake, Partner API) are screened, so covering a "
			"new channel is a config edit rather than a code change. Off, the native checks stand "
			"alone. The ClamAV container is needed for the scan.\n"
			"Example: with Partner API listed as active, an .exe renamed to .pdf and sent to "
			"file_attach is refused, and an infected upload is blocked."
		),
		# file_screening.screen is called by FileOverride.before_insert, an override_doctype_class seam and NOT a doc_event (as a hook it scanned bytes already on disk), so backs is empty — drift walks only doc_event/scheduler paths and this target stays OUT of the registry.
		backs=[],
		activator="tatva_connect.storage.file_screening.apply_scan_logging",
	),
	Auto(
		key="Partner::Catalog::cache",
		fires_on="Doc Event",
		trigger_detail="CRM Lead API Field · on_update + on_trash",
		purpose=(
			"The partner-API field catalog is refreshed the moment its definition changes, so an "
			"external partner always fetches the current list of fields. Off, a partner is served the "
			"cached list until it expires of its own accord.\n"
			"Example: an admin adds a new API field, and the partner's very next call reflects it "
			"instead of returning a stale catalog."
		),
		backs=["tatva_connect.api.partner.clear_catalog_cache"],
	),
	Auto(
		key="Partner::RateLimit::enforcement",
		fires_on="Provider call",
		trigger_detail="api/_base · @_api + _run_bulk gate",
		purpose=(
			"The partner API is throttled, so that no one partner — and no flood of them together — "
			"can starve the shared web workers that also serve the CRM. Off, nothing is throttled and "
			"the API behaves exactly as it does today.\n"
			"Example: a partner script looping too fast is answered with a 429 and a Retry-After, and "
			"the CRM stays responsive for everyone else."
		),
		# A gate inside _base (@_api + _run_bulk), NOT a doc_event/scheduler — like
		# Telephony/Location/visibility, so backs is empty (drift walks only doc_events
		# + scheduler paths; this gate's target stays OUT of the registry).
		backs=[],
	),
	Auto(
		key="Partner::AsyncBulk::jobs",
		fires_on="Provider call",
		trigger_detail="api/partner_bulk_job · submit gate",
		purpose=(
			"The asynchronous bulk-job tier is opened up: a partner may submit a high-volume create job, "
			"which a dedicated background worker drains through the same create brain the sync endpoints "
			"use, reporting completion by webhook and a status endpoint. Off, a submit is refused and no "
			"job runs; the synchronous single and slim-bulk endpoints are unaffected either way.\n"
			"Example: a partner posts a 10,000-lead job, receives a job id at once, and polls it to "
			"JobComplete while the CRM stays responsive."
		),
		# The submit gate lives inside api/partner_bulk_job (not a doc_event), so the toggle has no
		# doc_event target; the tier's one doc_event is the always-on SSRF guard on a partner's
		# completion-webhook URL, covered here so the drift lock passes.
		backs=["tatva_connect.api.partner_bulk_job.guard_webhook_url"],
	),
	Auto(
		key="Lead::BulkImport::desk",
		fires_on="Doc Event",
		trigger_detail="lead_import/api · validate + import gate; CRM Bulk Job · on_update",
		purpose=(
			"Leads are loaded into the CRM from a spreadsheet in the Desk: an operator picks the contract "
			"that carries the grain, maps each column to a section and a field, and the rows are written "
			"by the same brain the partner API writes through, on the bulk worker's own queue. A file is "
			"always validated first — every row is written and rolled back — so nothing lands until the "
			"result has been read. Off, neither validation nor import runs and the form is inert.\n"
			"Example: a clinic sends 1,800 patients as an Excel file; it is mapped once, validated to show "
			"11 refusals, and the remaining rows are loaded without touching the web workers."
		),
		# The gate lives inside lead_import/api (_queue), not a doc_event; the surface's one doc_event is
		# the status mirror from a finished job back onto its import, covered here so the drift lock passes.
		backs=["tatva_connect.lead_import.api.follow_job_status"],
	),
	Auto(
		key="Partner::AsyncBulk::reaper",
		fires_on="Schedule",
		trigger_detail="hourly at :45",
		purpose=(
			"An async bulk job left in-progress past its worker timeout — because the worker was "
			"killed, redeployed or ran out of memory — is marked failed and its stored upload dropped, "
			"so it cannot sit half-done forever and the partner is told by the completion webhook. Off, "
			"a stranded job stays in-progress until it is cleared by hand.\n"
			"Example: the queue worker is restarted mid-drain, and within the hour the job is failed "
			"with its payload purged."
		),
		backs=["tatva_connect.api.partner_bulk_worker.reap_stranded_jobs"],
	),
	Auto(
		key="Partner::AsyncBulk::purge",
		fires_on="Schedule",
		trigger_detail="daily 04:15",
		purpose=(
			"Finished async bulk jobs — their per-record results and the uploaded payload — are dropped "
			"each day once past the retention window, so the tables and blob store do not grow without "
			"bound. Off, finished jobs and their uploads are kept indefinitely.\n"
			"Example: a job that completed eight days ago (retention seven) is removed at 04:15, results "
			"and file included."
		),
		backs=["tatva_connect.api.partner_bulk_job.purge_expired_jobs"],
	),
	Auto(
		key="Observability::Requests::logging",
		fires_on="Provider call",
		trigger_detail="after_request · partner-API + inbound-webhook endpoints",
		purpose=(
			"Every partner-API call and inbound webhook is written to CRM API Request Log, which is "
			"what feeds the observability dashboard. Off, nothing is captured and the dashboard has "
			"nothing to show.\n"
			"Example: a partner's lead_create call is recorded with its endpoint, its status and its "
			"latency."
		),
		backs=["tatva_connect.observability.capture.log_request"],
		activator="tatva_connect.observability.capture.apply_logging",
	),
	Auto(
		key="Observability::Metrics::rollup",
		fires_on="Schedule",
		trigger_detail="every 6h",
		purpose=(
			"The raw request log is rolled up into compact metric tables every six hours, so "
			"dashboards and health checks are answered from a summary instead of the database being "
			"bloated by their queries. Off, the charts hold whatever the last rollup left behind.\n"
			"Example: the request-volume and latency charts on the operations dashboard refresh on "
			"this schedule."
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
			"A Word, Excel or PowerPoint file shared from SharePoint or OneDrive is shown as a "
			"readable panel inside the training lesson, the way a Google Doc or Slides link already "
			"is, so training material need not be converted or re-uploaded to be used in a course. "
			"The lesson stores the link exactly as the author typed it and the panel is built each "
			"time the lesson is opened, so nothing an author saves can undo it. The document is "
			"shown read-only and must be shared so that anyone holding the link can open it. Off, "
			"the reader sees the plain link.\n"
			"Example: a course author pastes a SharePoint link to a training deck into a lesson, and "
			"learners see the deck laid out in the lesson itself."
		),
		# An OVERRIDE target, not a doc_event or a scheduled job, so `backs` is empty — the shape the AI Voice and Transcription channel rows already have.
		backs=[],
	),
	Auto(
		key="Search::Index::indexing",
		fires_on="Doc Event",
		trigger_detail="frappe sqlite_search · index-on-save + query gate",
		purpose=(
			"The global spotlight search is switched on: leads, notes, tasks, call logs and file names "
			"are held in a full-text index, and a record is added to it as it is saved and dropped as it "
			"is deleted, so a rep can jump to a patient by name, number or id from anywhere. On enable, "
			"the whole existing set is indexed once in the background; a search only ever returns the "
			"records the caller may already see. Off, which is how it ships, nothing is indexed and the "
			"search returns nothing — the state to hold through a data migration, so imported records "
			"cost nothing, before it is switched on against the settled data.\n"
			"Example: a rep types a patient's mobile number into the search box and is taken straight to "
			"that lead, without opening a single list or filter."
		),
		# A gate read by is_search_enabled; per-save indexing rides frappe's own sqlite_search doc_events. The four below are OURS: the index denormalises each lead's owner/assignee/share set into a permission column, so every mechanism that moves it restamps the lead + its child rows. The activator builds the index once on enable.
		activator="tatva_connect.search.activation.apply",
		backs=[
			"tatva_connect.search.index.reindex_on_lead_context_change",
			"tatva_connect.search.index.reindex_on_assignment",
			"tatva_connect.search.index.reindex_on_assignment_change",
			"tatva_connect.search.index.reindex_on_share",
			# Hourly: drops an index that can no longer be READ, the one damaged state frappe's own 3-hourly check cannot see — it asks whether the file exists, not whether it opens. Gated here because a disabled feature has no index to keep healthy.
			"tatva_connect.search.index.sweep_index_health",
		],
	),
	Auto(
		key="Activity::Timeline::indexing",
		fires_on="Doc Event",
		trigger_detail="activity/timeline · pointer written on save, dropped on delete",
		purpose=(
			"The lead's Activity rail is served from an index instead of being assembled on every open: "
			"as a call, note, task, file, comment or email is saved against a lead, a single line "
			"recording which record it was and when is added to a list held per lead, and that line is "
			"removed as the record is deleted. The rail then reads one page of that list, so opening a "
			"lead with thousands of entries costs the same as opening a new one, and a rail page stays "
			"fast as a patient's history grows. On enable, the list is built once for every existing "
			"lead in the background. Off, which is how it ships, the rail is assembled from every source "
			"at the moment it is opened, exactly as it is today — nothing is written and nothing is "
			"read. Nothing in the list is a record in its own right: it names rows that already exist "
			"and is rebuilt from them, so it can be switched off, on, or repaired without any loss.\n"
			"Example: a rep opens a patient followed for two years and the Activity tab paints at once, "
			"instead of pausing while every call, task and note ever logged is gathered."
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
			"Words the system already knows — a stage, a vertical, a group, a person — are read out of a "
			"typed search and applied as filters, and only what is left over is searched as text, so "
			"'onco kavita' narrows to the Onco patients called Kavita instead of asking the index for both "
			"words at once. Nothing recognised, and the search behaves exactly as it does with this off. "
			"Off, which is how it ships, every typed word goes to the full-text index and the search box "
			"reports no interpretation.\n"
			"Example: a rep types a vertical and a patient's first name together, and the result list is "
			"narrowed to that vertical before the name is matched."
		),
		requires="Search::Index::indexing",
	),
]

# Import-time graph gate: a `requires` naming a dead key, or a cycle, cannot reach the catalog.
_PARENT_OF = assert_valid_graph(AUTOMATIONS)


def parent_of(key):
	"""The ONE parent lookup — the key this row requires, or `""` when it stands alone."""
	return _PARENT_OF.get(key, "")


def area_of(key):
	"""The ONE area derivation — segment 1 of the key, as this module's docstring declares."""
	return (key or "").split("::")[0]


def activator_for(key):
	for auto in AUTOMATIONS:
		if auto.key == key:
			return auto.activator
	return ""
