"""The Monitor paragraph names the two lists an operator can actually see.

Declared end state: the Automations workspace's Monitor prose says <b>Journeys</b> and <b>Signals</b> —
the labels its own links carry — and calls what parks and resumes a JOURNEY.

WHAT WAS WRONG. W5.1 renamed the doctypes and relabelled the links (Runs -> Journeys, Events -> Signals)
but left the paragraph beside them reading "Running Instances" and "Signal Inbox". An operator was told to
look for two sections that are not on the screen. The same sentences also called the runtime thing a
"Flow" twice — a Flow is what is AUTHORED; what parks and executes is a journey and its steps.

DELIBERATELY NOT TOUCHED: the other seventeen uses of "Flow" in this file. It is the file that DEFINES
"a rule is a Flow that runs once; a journey is a Flow that waits and resumes", and whether the authored
thing stays a Flow or becomes a Workflow is an open product-owner decision, not this patch's.

WHY A PATCH AT ALL. `import_file.py:141` skips a standard JSON whose DB row is not older than the file, so
on any site whose desk was ever opened the edit migrates silently green and changes nothing. The bumped
`modified` is necessary and not sufficient. Through `patches/_desk.reimport`, never a hand-rolled
`import_file_by_path(force=True)`: force DELETES and re-inserts, and on a developer_mode site that rmtree's
the exported folder while the re-insert's export returns early — the patch eats its own JSON.

Only the workspace is reimported. The sidebar is untouched by this change, and reimporting a file that did
not change is a delete-and-re-insert for no reason.

A fresh site baselines this line without running it: it is born with the corrected prose.
"""
from tatva_connect.patches import _desk

DESKS = (("tatva_connect", "workspace", "automations", "automations.json"),)


def execute():
	_desk.reimport_all(DESKS)
