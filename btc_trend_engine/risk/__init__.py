"""Risk decision layer for the trend engine (Trend_Engine.md §14, ADR 0003).

``risk_manager.py`` is the single entry point; everything else in this
package is a component it composes. Account-limit evaluation and sizing are
never reimplemented here — ``risk_controls.py`` (repo root) remains the one
source of truth for both, per ADR 0003's "single source of truth" decision.
"""
