"""The channel layer — what a messaging channel is, independent of who carries it.

A CHANNEL is the thing the business talks about: WhatsApp. A PROVIDER is whoever moves the bytes
today: WATI. The two were fused for as long as there was one vendor, and every fusion point became a
place a second vendor could not be added without a second brain — a vendor in the URL, a vendor in the
switch key, a vendor in the registry key, a vendor in a class name.

This package holds the vendor-free half:

  * `contract`  — what an adapter DECLARES (channel · provider · account doctype · the outcomes it can
                  truthfully emit · the capabilities it has) and the one shape a send returns.
  * `event`     — `ChannelEvent`, the ONE normalized event every adapter emits. Nothing downstream
                  reads a provider's field name, and no emitted event ever carries a vendor's name.
  * `resolve`   — the adapter for an account, from the ONE registry (`webhooks.registry`).

An adapter is reached one way — `resolve.adapter_for(account)` — and its declaration one way,
`adapter_for(account).DECLARATION`. Nobody re-derives it and nobody keeps a second copy.
"""
from tatva_connect.channels.contract import CAPABILITIES, Declaration, SendResult
from tatva_connect.channels.event import KINDS, OUTCOMES, ChannelEvent, build, event_name
from tatva_connect.channels.resolve import adapter_for, adapter_for_channel, has_adapter

__all__ = [
	"CAPABILITIES",
	"Declaration",
	"SendResult",
	"KINDS",
	"OUTCOMES",
	"ChannelEvent",
	"build",
	"event_name",
	"adapter_for",
	"adapter_for_channel",
	"has_adapter",
]
