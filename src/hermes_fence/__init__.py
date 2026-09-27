"""hermes-fence — plugin surface.

Layering is deliberate: `store` / `locks` / `sync` / `mux` / `fences` are pure state and
know nothing about Hermes, so they are testable without the framework. *This* module is the
only place that knows how to become an agent.
"""

__version__ = "0.2.0"
