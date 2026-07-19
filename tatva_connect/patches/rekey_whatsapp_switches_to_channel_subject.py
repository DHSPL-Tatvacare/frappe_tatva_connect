"""Repair the benches that already ran the interim two-part rekey.

`rekey_whatsapp_switches_to_channel` is applied on every dev bench, and the revision that ran there
landed TWO-part keys (`WhatsApp::messaging`). Every other key on this site is
`Area::Subject::Capability`, and the shape is now enforced (`registry.assert_valid_key`), so those
three rows are the only malformed ones on the site. Rule 3 of patches.txt: an applied patch is dead —
editing it cannot repair a site that already ran it, so the repair ships as this new line.

End state, identical to the patch it follows: exactly the three `WhatsApp::Channel::*` rows exist,
carrying the operator's tuned `enabled`, with no two-part or vendor-scoped predecessor left behind.
The mapping and the merge rule are NOT restated here — they are declared once, in that patch, and
re-asserted by calling it. A no-op on any site already in the end state, and safe to run twice.
"""
from tatva_connect.patches import rekey_whatsapp_switches_to_channel


def execute():
	rekey_whatsapp_switches_to_channel.execute()
