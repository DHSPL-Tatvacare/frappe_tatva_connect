"""The workflow engine's two operator kill switches, dormant by default (constitution A.6).

`ENGINE_SWITCH` gates the whole engine: entry-trigger starts, signal delivery, and every wake. `SWEEP_SWITCH`
gates the scheduled timer+reconciler sweep and DECLARES the engine switch as its parent, so `is_enabled`
answers for both and no caller checks the pair by hand — an operator can still pause the sweep without
killing the engine. Both ship OFF; the operator turns them on.
"""
ENGINE_SWITCH = "Workflow::Engine::run"
SWEEP_SWITCH = "Workflow::Engine::sweep"
