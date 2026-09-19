"""The workflow engine's operator kill switch, dormant by default (constitution A.6).

`ENGINE_SWITCH` gates the whole engine: entry-trigger starts, signal delivery, every wake and the */15
backstop. It ships OFF; the operator turns it on.
"""
ENGINE_SWITCH = "Workflow::Engine::run"
