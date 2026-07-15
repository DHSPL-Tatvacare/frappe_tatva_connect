"""The workflow engine's two operator kill switches, dormant by default (constitution A.6).

`ENGINE_SWITCH` gates the whole engine: entry-trigger starts, signal delivery, and every wake. `SWEEP_SWITCH`
gates just the scheduled timer+reconciler sweep, so an operator can pause the sweep without killing the
engine (mirrors the automation engine's `rules`/`resume` split). Both ship OFF; the operator turns them on.
"""
ENGINE_SWITCH = "Workflow::Engine::run"
SWEEP_SWITCH = "Workflow::Engine::sweep"
