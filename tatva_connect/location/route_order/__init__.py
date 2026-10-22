"""Route order — the ONE seam that puts a day's stops in visiting order.

Every provider is a module with a `LABEL` (its option in CRM Maps Settings → Route Order) and
`order(origin, stops) -> contract.Ordered`. `origin` carries `lat`/`lng`; every stop carries `id`, `lat`, `lng`
and comes back as the caller's own object, so nothing about a provider leaks past its module. Callers reach
a provider only through `resolve.order`, which reads the setting, gates Google, falls back to straight line,
and remembers the order so a plan is the same on every read and Google is asked once per plan.
"""
