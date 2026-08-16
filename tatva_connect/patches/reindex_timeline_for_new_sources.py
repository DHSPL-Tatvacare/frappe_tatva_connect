"""Re-fill the Activity rail index, because `timeline.SOURCES` gained WhatsApp Message and Integration
Request and an index already switched ON holds no pointer for either.

WHY A PATCH IS NEEDED AT ALL. The index is filled on two occasions and no others: the doc_event hooks, as
each new row is written, and `timeline.activate()`, when an operator switches the toggle on. Adding a SOURCE
is neither. On a site where the toggle is already on, every WhatsApp the engine has ever sent and every Call
API record it has ever written would be absent from the rail — silently and for ever, because nothing else
ever revisits history. Nothing is WRONG on such a site; rows are simply missing, which is the failure a
reader cannot see.

WHY IT IS SAFE. `build_all` is `rebuild` per lead, and `rebuild` clears that lead's pointers before
regenerating them from source — so this repairs rather than duplicates, and running it twice is running it
once. The index stores no content, only a pointer, so a regenerated row is identical to the one it replaces.

WHY IT IS A NO-OP ALMOST EVERYWHERE. The toggle ships dormant. A site with the index off serves the rail
from the read-time merge, which reads the source tables directly and therefore already shows both new
subjects the moment the code lands — so there is nothing to fill, and this exits before touching anything.
A fresh install is the same case: no toggle, no rows, nothing to backfill.

ENQUEUED, NEVER INLINE — the same call `activate()` makes, for the same reason: a pass over every lead on the
site must not hold up a migrate. No DDL, so no schema_setup twin; a fresh site has no history to reindex."""

import frappe

from tatva_connect.activity import timeline
from tatva_connect.automation.settings import is_enabled


def execute():
	if not frappe.db.table_exists("CRM Timeline Event"):
		return
	# Off means the rail reads the source tables directly and is already complete. Nothing to fill.
	if not is_enabled(timeline.TOGGLE):
		return
	frappe.enqueue(
		"tatva_connect.activity.timeline.build_all",
		queue="long",
		timeout=14400,
		enqueue_after_commit=True,
	)
