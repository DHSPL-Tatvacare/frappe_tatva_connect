"""Apply idempotent SCHEMA patches on after_migrate.

`install-app` BASELINES patches.txt without running it (see seeds.py), so structural
changes done via patches — indexes, Select options, custom fields — never land on a
fresh DB. These are idempotent, so we also run them on after_migrate: a no-op on an
existing DB that already has them, and the thing that actually builds them on a clean
install. They stay in patches.txt too (existing-DB ordering + history).

Only TWO things genuinely belong here — structures we can't put in a file:
  * the composite WhatsApp index — the `WhatsApp Message` doctype belongs to the upstream
    frappe_whatsapp app (no-fork rule), so we can't declare the index in its JSON.
  * the Acefone telephony medium — appends "Acefone" to a Select on frappe/crm's own
    doctypes; a fixture would REPLACE their options (and drift across crm versions), so we
    merge in code instead of clobbering.
(The Azure File marker is a plain custom field, so it ships as a fixture, not here.)
Each step isolates its own failure (rollback + log) so one gap never aborts the migrate.
"""
import frappe
from frappe import _

from tatva_connect.patches import (
	add_acefone_telephony_medium,
	add_ai_voice_telephony_medium,
	add_call_log_reference_index,
	add_call_media_recording_index,
	add_clinic_anchor_index,
	add_task_due_state_index,
	add_crm_task_metrics_index,
	add_integration_request_index,
	add_lead_dedup_unique_index,
	add_lead_timeline_indexes,
	add_observability_indexes,
	add_task_answer_fieldname_index,
	add_task_answer_question_index,
	add_task_document_kind_index,
	add_task_lead_snapshot_index,
	add_timeline_paging_indexes,
	add_workflow_due_index,
	backfill_webhook_token_digests,
	build_lead_timeline_index,
	hash_name_transactional_doctypes,
	migrate_webhook_tokens_to_password,
	recreate_whatsapp_message_id_index_composite,
	reindex_screening_answers_by_hash,
	rekey_task_types_composite,
	retire_activity_legacy_columns,
	retire_lead_import_coordinates,
	retire_lead_stage_legacy_fields,
	retire_location_captures_fields,
	retire_nearme_map_provider,
	retire_task_slot_columns,
)

_STEPS = (
	recreate_whatsapp_message_id_index_composite,
	add_acefone_telephony_medium,
	# "AI Voice" on CRM Call Log.telephony_medium — an AI call lands in the same MIXED table as Acefone's and a rep's, and the medium is what tells them apart. A Property Setter because stock options drift between crm versions; install-app baselines its patch without running it.
	add_ai_voice_telephony_medium,
	retire_location_captures_fields,
	retire_lead_stage_legacy_fields,
	retire_activity_legacy_columns,
	add_observability_indexes,
	add_crm_task_metrics_index,
	# (status, due_date) on CRM Task — every due-state predicate and every team_charts due count seek on
	# that pair; the existing indexes lead with reference_docname and cannot serve it. Composite, so not
	# JSON-declarable, and install-app baselines its patch without running it.
	add_task_due_state_index,
	# (service, status) on frappe's Integration Request — the DLQ replay and every Desk filter select on both, and frappe declares no index on a table it keeps for 90 days.
	add_integration_request_index,
	# UNIQUE (mobile_no, custom_vertical, custom_group) on CRM Lead — the partner API's dedup rule; a composite unique cannot be declared in crm's JSON, so this is its only fresh-install path.
	add_lead_dedup_unique_index,
	# (reference_doctype, reference_docname) on CRM Call Log — call_list and every Desk reference filter select on both; composite, so not JSON-declarable.
	add_call_log_reference_index,
	# Re-key CRM Task Type to grain-scoped composite keys (ADR); runs after the doctype JSON sync adds the parent grain fields, idempotent, and cascades the custom_task_type Link.
	rekey_task_types_composite,
	# A naming_series name is minted from ONE tabSeries row whose lock is held to commit, so concurrent creates deadlock (1 of 32 survived a 32-way burst; the partner API turned that into 124 HTTP 500s). A patch alone would never reach a fresh site — install-app baselines it — so the rule is applied here too. Idempotent: it skips a doctype already named by hash.
	hash_name_transactional_doctypes,
	# Carry existing webhook tokens into the Password store after the Data->Password flip; a fresh install has blank tokens (clean no-op).
	migrate_webhook_tokens_to_password,
	# Inbound auth resolves an account by digest in one indexed read; runs AFTER the token carry above, which is what puts a token in the Password store to digest.
	backfill_webhook_token_digests,
	# CRM Lead's dead custom_latitude/custom_longitude pair duplicated the clinic anchor; the fixture sync never drops a field, so remove it here too.
	retire_lead_import_coordinates,
	# Composite (clinic lat, clinic lng) — Near Me's bounding-box prefilter scanned the table without it.
	add_clinic_anchor_index,
	# (reference_docname, creation) on FCRM Note and CRM Call Log — the lead's Notes and Calls tabs filter on docname ALONE, so the note table full-scanned and the call log full-index-scanned (its own index leads with reference_doctype). Composite, so not JSON-declarable.
	add_lead_timeline_indexes,
	# (reference_docname, creation) on CRM Task and (attached_to_name, creation) on File — the last two timeline reads that scanned. Both ABANDONED their existing index for a full walk of creation once a page was ordered and limited: CRM Task's leads with reference_docname but carries task_type/status next (cannot serve the sort), File's leads with attached_to_doctype (cannot seek a non-leading column). Composite, so not JSON-declarable.
	add_timeline_paging_indexes,
	# (reference_doctype, reference_name, event_on) + UNIQUE (source_doctype, source_name) on CRM Timeline Event, then fill it from source. Composite, so not JSON-declarable, and the unique pair is what makes the fill re-runnable. The rail is one seek on this table instead of a read-time merge across six.
	build_lead_timeline_index,
	# (trigger_mode, trigger_next_run_at) on CRM Workflow — the cohort drain's one question, equality then range. Composite, so not JSON-declarable, and install-app baselines its patch without running it.
	add_workflow_due_index,
	# (recording_state, recording_next_attempt_at) on CRM Call Media — the media sweep's one question, equality then range. Composite, so not JSON-declarable, and install-app baselines its patch without running it.
	add_call_media_recording_index,
	# (question, parent) on the activity key-value table — the Smart View reads ONE question across every task, which the form's (parent, question) index cannot seek. Reads the section declaration; no-ops until it is seeded.
	add_task_answer_question_index,
	# Near Me is one Google map now; its provider Select is gone and its dead Singles value with it.
	retire_nearme_map_provider,
	# (parent, question_hash) and (question_hash, value) on CRM Lead Screening Answer — the Data tab read and the Smart View join select on them; a fresh site would otherwise full-scan forever.
	reindex_screening_answers_by_hash,
	# (parent, document_kind) on CRM Task Document — the Documents section is multi-row keyed by document_kind and every read picks one task's latest row of a kind; composite, so not JSON-declarable, and frappe's own (parent) index is left alone.
	add_task_document_kind_index,
	# (parent, fieldname) on CRM Task Answer — the key-value section is one row per DECLARED field per task, so it is the widest child table here and every read addresses one task's row for one fieldname; composite, so not JSON-declarable.
	add_task_answer_fieldname_index,
	# (parent, fieldname) on CRM Task Lead Snapshot — the second key-value table, same read and same shape as the answers one above.
	add_task_lead_snapshot_index,
	# Phase 7: drop the five dead CRM Task slot columns + the JSON payload once every answer they held is homed in a section row. NOT here for the fresh-install reason an index has — the fixture no longer declares them, so a new site never grows them. It is here because the patch REFUSES while a site is un-backfilled, and an applied patch is dead: this pass re-asserts the end state every migrate, so the run after the after_migrate backfill is the one that drops.
	retire_task_slot_columns,
)


def apply_schema():
	failures = []
	for mod in _STEPS:
		try:
			mod.execute()
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(frappe.get_traceback(), f"apply_schema: {mod.__name__}")
			failures.append(mod.__name__)
	_ensure_fixed_settings()
	_ensure_field_map_role()
	_ensure_new_modules()
	_assert_observability_bands()
	if failures:
		# Each step isolates its own failure above (rollback + log) so the rest still run — but a real
		# structural gap must NOT pass as a green migrate. The throw is DEFERRED to assert_schema_applied,
		# wired LAST in the after_migrate/after_sync chain, because this function is the SECOND entry of
		# twenty-four: throwing here skipped every seed, every drift assert and the lockdown behind it, and
		# a fresh site came up with no sections, no switches and no lockdown off ONE failed index.
		# COMMIT FIRST: this runs inside @atomic post_schema_updates, so without it a later throw rolls the whole migrate back — INCLUDING the Patch Log rows written earlier in the same run, which made every retry re-run all 52 patches from scratch and nothing ever record as done.
		frappe.db.commit()
		frappe.flags.tc_schema_failures = list(failures)
		print("apply_schema: FAILED for {0} — the chain continues; assert_schema_applied will fail the run at the end.".format(", ".join(failures)))
	else:
		frappe.flags.tc_schema_failures = []


def assert_schema_applied():
	"""Fail the migrate if apply_schema could not land a structural step. LAST in the chain, so a broken
	index no longer costs a site its seeds — everything else has already run by the time this throws."""
	failures = getattr(frappe.flags, "tc_schema_failures", None)
	if failures:
		frappe.db.commit()
		frappe.throw(_("Schema setup failed for: {0}. See Error Log for tracebacks.").format(", ".join(failures)))


def _ensure_new_modules():
	"""Create a Module Def + sync its doctypes for any module in modules.txt missing one.

	install-app reads modules.txt and creates every Module Def up front, so a FRESH install is
	fine. But adding a NEW module to an already-installed app: the in-place `migrate` doctype-sync
	runs BEFORE after_migrate and skips a module whose Module Def doesn't yet exist — so its
	doctype JSONs never land. This guard (after_migrate) creates the missing Module Def and imports
	that module's doctype files on the SAME migrate, so a new module builds on first migrate too.

	The end state is "every declared doctype EXISTS", not "the Module Def exists". Keying it on the
	Module Def made this a one-shot that could never self-heal: the Def is committed before the
	imports, so an import that failed left the Def behind, and every later migrate skipped the module
	and never retried the doctype. That is how CRM Lead Import stayed missing across eight deploys.
	Isolates its own failure, per module, so one bad module cannot cost the others theirs."""
	import os

	from frappe.modules.import_file import import_file_by_path

	app_path = frappe.get_app_path("tatva_connect")
	modules = [m.strip() for m in (frappe.get_module_list("tatva_connect") or []) if m.strip()]
	for module in modules:
		try:
			if not frappe.db.exists("Module Def", module):
				md = frappe.new_doc("Module Def")
				md.module_name = module
				md.app_name = "tatva_connect"
				md.insert(ignore_permissions=True)  # authz-ok: tier-a — schema setup, runs at migrate
				frappe.db.commit()

			# The module->app map is built from a REDIS-cached modules.txt snapshot, not from Module Def.
			# A worker that booted on the previous image caches a map with no entry for a module added
			# since, and DocType.on_update -> run_module_method -> get_module_app then throws
			# "Module X not found" on import. The row exists, the map does not. Rebuild it from disk
			# before importing, or the doctype can never land on a site whose cache predates the module.
			if frappe.scrub(module) not in (frappe.local.module_app or {}):
				frappe.cache.delete_value("app_modules")
				frappe.client_cache.delete_value("installed_app_modules")
				frappe.setup_module_map()

			module_dir = os.path.join(app_path, frappe.scrub(module))
			dt_dir = os.path.join(module_dir, "doctype")
			if not os.path.isdir(module_dir):
				# The module's own folder is not in the RUNNING IMAGE — a packaging failure whose first consumer dies far from the cause. Silence is what made this cost eight deploys.
				frappe.log_error(title="apply_schema: module folder absent from this image", message=f"Module '{module}' is in modules.txt but {module_dir} does not exist here.")
				continue
			if not os.path.isdir(dt_dir):
				continue  # a CODE-ONLY module — `Access` owns rules, not doctypes; there is nothing to land and no alarm to raise

			# Two passes: listdir order is arbitrary, so a parent can be reached before the child table
			# doctype it declares. The first pass lands whatever it can, the second retries the rest.
			missing = _missing_doctypes(dt_dir)
			for _pass in (1, 2):
				if not missing:
					break
				for json_path in missing:
					try:
						import_file_by_path(json_path, force=True)
					except Exception:
						frappe.db.rollback()
				frappe.db.commit()
				missing = _missing_doctypes(dt_dir)

			if missing:
				# Loud. A declared doctype that is still absent kills the first consumer that Links to it,
				# far from this cause — which is exactly how client_scripts_seed died on CRM Lead Import.
				frappe.log_error(
					title="apply_schema: declared doctypes did not land",
					message=f"Module '{module}' still has no DocType row for:\n" + "\n".join(missing),
				)
		except Exception:
			frappe.db.rollback()
			frappe.log_error(frappe.get_traceback(), f"apply_schema: ensure_new_modules ({module})")


def _missing_doctypes(dt_dir):
	"""The doctype JSONs in this folder with no DocType row yet — read from the file, never guessed."""
	import json
	import os

	out = []
	for dt_folder in sorted(os.listdir(dt_dir)):
		json_path = os.path.join(dt_dir, dt_folder, dt_folder + ".json")
		if not os.path.exists(json_path):
			continue
		try:
			with open(json_path) as f:
				name = json.load(f).get("name")
		except Exception:  # nosec B112 — an unreadable/!DocType json is not a doctype to land; the caller only needs the ones that are
			continue
		if name and not frappe.db.exists("DocType", name):
			out.append(json_path)
	return out


def _assert_observability_bands():
	"""Fail loudly if the latency-band single-source-of-truth drifts from the CRM API Metric
	columns (a new band added to LATENCY_BANDS without a matching column, or vice versa)."""
	try:
		from tatva_connect.observability.constants import assert_bands_match_schema

		assert_bands_match_schema()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "apply_schema: observability bands")


def _ensure_field_map_role():
	"""Seed the `Field Map User` role — the single gate for the Near Me directory page in the CRM
	fork. The role's EXISTENCE is structural to the feature (identical in every deployment), so it
	belongs here; ASSIGNING it to users is operator business data and is NEVER done in code. The
	page also requires the `Location::NearMe::directory` switch ON, so a seeded-but-unassigned role
	plus an off switch keeps the feature fully dormant (CLAUDE.md #4/#6). Idempotent; isolated."""
	try:
		if not frappe.db.exists("Role", "Field Map User"):
			role = frappe.new_doc("Role")
			role.role_name = "Field Map User"
			role.desk_access = 0  # SPA-only role; no Desk surface
			role.save(ignore_permissions=True)  # authz-ok: tier-a — schema setup, runs at migrate
			frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "apply_schema: ensure_field_map_role")


def _ensure_fixed_settings():
	"""Backfill display-only fixed values on single settings (a field default never reaches an existing
	single doc). geocoding_provider is always Google — shown grayed in CRM Maps Settings for reference.
	Idempotent; isolates its own failure so it never aborts the migrate."""
	try:
		if frappe.db.exists("DocType", "CRM Maps Settings"):
			frappe.db.set_single_value("CRM Maps Settings", "geocoding_provider", "Google")
			frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "apply_schema: ensure_fixed_settings")
