"""Deterministic replay fixtures for the eleven §23.3 scenarios.

These are *synthetic* by design. Recorded production data does not yet span
the interesting cases (Phase 2 capture started recently, and a liquidation
cascade cannot be summoned on demand), and a fixture built from real data
would also drift as retention prunes it. Synthetic series are reproducible
forever and let each scenario isolate exactly one failure mode.

The trade-off is stated rather than hidden: these prove the engine *responds
correctly to a described condition*. They do not prove the condition looks
like this in the wild. Replacing them with recorded equivalents is Phase 8
work, once the archive is long enough to contain real examples.
"""

from .fixtures import SCENARIOS, Scenario, scenario  # noqa: F401
