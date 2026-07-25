"""Pure order-lifecycle models and validators (ADR 0003).

Nothing in this package can place, amend or cancel an order. It emits and
validates data only; ``trend_score_live_execution.py`` remains the sole
process that POSTs to ``/v2/orders``.
"""
