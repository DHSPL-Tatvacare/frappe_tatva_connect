"""P9 re-point: force the (content-changed) Lead API Field catalog seed to re-run.

Frappe runs a patch once BY NAME, so the substantive P9 rewrite of
`seed_lead_api_fields` (adds `is_row_key`, the `acq:*`/`drug:*` sections, and
de-lists `plan:substage`/`plan:priority_text`) would NOT re-apply on an environment
where the original seed already ran. This uniquely-named one-time patch simply
re-invokes the (idempotent) seed so existing installs pick up the new catalog. On a
fresh install both run and the seed's idempotency makes the second call a no-op.
"""
from tatva_connect.patches.seed_lead_api_fields import execute as seed_execute


def execute():
	seed_execute()
