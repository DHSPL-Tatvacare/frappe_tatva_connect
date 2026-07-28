# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""What a call's ARTIFACTS are — one row per call, whoever produced them.

ARTIFACT STATE, NEVER CALL FACTS. Who was called, when, how long it ran and how it ended all live on
`CRM Call Log` and are never copied here: two rows carrying the same fact is how they start to disagree.
This row answers only "what do we hold for this call, and what are we still waiting for".

NOT A COLUMN ON THE CALL LOG, deliberately. That table is hot, mixed and upstream: the Calls tab, the
lead timeline index and telephony's reconciler all read it, and it carries no text blob today. It is
also an upstream doctype, so anything added to it arrives as a fixture — and fixtures land AFTER
post-model-sync patches, which is the ordering trap that has cost this app real deploys. Our own row has
neither problem.

ONE SHAPE WITH OPTIONAL PARTS. `segments` is `[{speaker?, start?, end?, text}]` and sparseness is the
whole trick — plain text is segments with no speaker and no times, one producer gives speaker without
times, another times without speaker, a third both. A new producer fills in fewer boxes; it never
adds a shape, and the reader renders the richest view its data supports.

`call` is also the NAME (`autoname: field:call`), so a lookup is a primary-key seek and one call can
never grow two media rows. The one query that is NOT a seek — the sweep's "which calls are still waiting
for audio" — is served by a composite index on (recording_state, recording_next_attempt_at), declared in
`patches/add_call_media_recording_index.py` and in `schema_setup._STEPS`.
"""
from frappe.model.document import Document


class CRMCallMedia(Document):
	pass
