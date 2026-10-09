"""BTC Trend Engine — regime-aware market-state and trend-signal service.

Built against ``Trend_Engine.md`` and the decisions in ``docs/adr/``.  The
package owns market-data ingestion, data-quality validation, feature
computation, regime classification and TrendSnapshot generation.  It holds no
trading credentials, never touches ``users/``, and cannot place an order.
"""

__version__ = "0.6.4"  # ADX evidence uses the completed 15m setup candle.
ENGINE_SCHEMA_VERSION = "1.4.0"  # docs/trend-snapshot-contract.md
