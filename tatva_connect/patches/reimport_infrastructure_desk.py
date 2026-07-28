# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Force the Infrastructure workspace + sidebar back to what the app ships.

File screening was configurable but unreachable: `CRM File Screening Settings` and `CRM File Scan Log`
had no route from any workspace, while the go-live checklist told the operator to configure screening.
Both now sit under Storage, beside Azure Blob.

The standard import skips a workspace whose DB copy looks newer than its file (import_file.py:124), so
a site whose desk was ever touched keeps the old two-item Storage group and never sees the new links.
The bumped `modified` in the JSON covers a site that never diverged; this covers the ones that did.
Re-import both, force. Same reason and same shape as `reimport_communications_desk`.
"""
from tatva_connect.patches import _desk


def execute():
	_desk.reimport_all([
		("tatva_connect", "workspace", "infrastructure", "infrastructure.json"),
		("workspace_sidebar", "infrastructure.json"),
	])
