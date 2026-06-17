"""External-dependency adapters (LLM, web search).

Everything that touches the network lives behind these thin clients so the
graph nodes stay pure and testable, and so we can flip to deterministic
stubs (USE_STUBS=true) for offline runs and the e2e test.
"""
