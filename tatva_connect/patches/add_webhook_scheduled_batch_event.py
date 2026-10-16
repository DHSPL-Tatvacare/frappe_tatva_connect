"""Merge "scheduled_batch" into Webhook's `webhook_docevent` Select; a fixture would replace core's eight options."""

from tatva_connect.observability.batched_webhooks import EVENT
from tatva_connect.patches.add_acefone_telephony_medium import _add_option


def execute():
	_add_option("Webhook", "webhook_docevent", option=EVENT)
