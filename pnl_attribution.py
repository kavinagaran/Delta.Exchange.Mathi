"""Decompose realised option P&L into delta / gamma / vega / theta / residual.

Why this exists: the strategy trades options (2-step ITM calls and puts, ATM
MOVE straddles) but every measurement we have is on the *perpetual*
(ADR 0005).  When a trade loses money nothing currently distinguishes
"the direction call was wrong" from "direction was right but IV collapsed"
from "we were right but held through too much theta" from "we paid the
spread".  Those four have opposite fixes, so aggregate P&L cannot drive any
decision.  This module answers which one it was.

The module is deliberately pure: it performs no I/O, reads no credentials,
touches no ``users/`` state and imports nothing outside the standard library.
Callers pass in the two market observations (entry and exit) and receive an
auditable breakdown.  That keeps it safe to import from anywhere, including
the dashboard, and trivially testable.

Method.  Sequential revaluation ("P&L explain"), not a Taylor expansion.
The position is walked from its entry state to its exit state one variable
at a time, fully repriced at each step:

    theta  =  V(S0, s0, T1) - V(S0, s0, T0)      time only
    spot   =  V(S1, s0, T1) - V(S0, s0, T1)      then the underlying
    vega   =  V(S1, s1, T1) - V(S1, s0, T1)      then implied vol

The steps telescope, so they sum to the model price change exactly -- there
is no truncation error.  A second-order expansion was tried first and
rejected on measurement: adding a (correct, finite-difference-verified)
vanna/volga term made the fit *worse* on a 3% move with a 30% IV crush,
because the third-order terms it exposed were larger still.

The spot step is further split into a linear ``delta`` part and the
curvature remainder ``gamma``, so a short straddle's negative convexity
stays visible on its own line.

Any sequential attribution is order-dependent -- the spot/vol interaction is
credited to whichever step runs second.  Vol runs last deliberately, so the
vol term is measured at the *realised* spot: that is the reading wanted when
asking "was the direction right but IV moved against us?".

Because implied vol is inverted from the traded prices at both ends, the
revaluation reproduces both marks exactly.  ``residual`` therefore reduces
to exactly ``-fees`` -- verified against a live account, where the summed
residual matched summed USD commission to the cent.

A consequence worth knowing before reading the output: **the bid-ask spread
is not visible here.**  Entering at the offer simply looks like a slightly
higher implied vol and exiting at the bid like a lower one, so the spread is
absorbed into the vol step and reported under ``vega``.  Separating
execution cost from a genuine vol move needs the mid/mark quote at trade
time, which means capturing the option chain -- until then, treat ``vega``
as "vol move *and* spread paid".

Implied vol is inverted from the traded price rather than trusted from a
feed, because we do not record a chain yet.  Where the inversion is
ill-conditioned (vega ~ 0: deep ITM/OTM, or nearly expired) the attribution
is marked unreliable instead of returning a confident wrong number.

Units.  Everything is per-position USD, scaled by ``contract_value * lots``
and signed by direction, so ``Attribution.total`` reconciles exactly with the
realised P&L the Performance page already computes.  ``vega`` is per 1.00 of
volatility (i.e. 100 vol points); ``theta`` is per year.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

CALL = "call"
PUT = "put"
STRADDLE = "straddle"          # MOVE contracts: long a call AND a put at K

_VALID_KINDS = frozenset({CALL, PUT, STRADDLE})

# Below this vega (per 1.0 sigma, per unit notional) the price barely responds
# to volatility, so inverting a price into an IV amplifies noise without
# bound. Such trades still get a delta/gamma attribution; the vol-dependent
# terms are reported as unreliable.
_MIN_VEGA_FOR_IV = 1e-6
_IV_LOW, _IV_HIGH = 1e-4, 10.0     # 0.01% .. 1000% annualised
_IV_TOLERANCE = 1e-8
_IV_MAX_ITER = 200

# On synthetic, model-consistent prices the residual is ~0. On real fills it
# is not, and should not be: entry and exit cross opposite sides of the
# bid-ask, so the two marks genuinely disagree under any single model. That
# difference IS the execution cost and is worth reading, not suppressing.
# This guard therefore only catches the implausible case -- an unexplained
# amount larger than the entire move, which means a bad mark, not a spread.
_MAX_RESIDUAL_FRACTION = 1.0


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1_d2(spot: float, strike: float, t_years: float,
           sigma: float, rate: float) -> tuple[float, float]:
    vol_sqrt_t = sigma * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * t_years) / vol_sqrt_t
    return d1, d1 - vol_sqrt_t


def _validate(kind: str, spot: float, strike: float,
              t_years: float, sigma: float) -> None:
    if kind not in _VALID_KINDS:
        raise ValueError(f"unsupported option kind: {kind!r}")
    if spot <= 0 or strike <= 0:
        raise ValueError("spot and strike must be positive")
    if t_years <= 0:
        raise ValueError("time to expiry must be positive")
    if sigma <= 0:
        raise ValueError("sigma must be positive")


def bs_price(kind: str, spot: float, strike: float, t_years: float,
             sigma: float, rate: float = 0.0) -> float:
    """Black-Scholes value per unit of underlying.

    ``rate`` defaults to 0: these are cash-settled crypto options where the
    carry shows up in funding/basis rather than a risk-free rate, and a
    fabricated non-zero rate would bias every greek.
    """
    _validate(kind, spot, strike, t_years, sigma)
    if kind == STRADDLE:
        return (bs_price(CALL, spot, strike, t_years, sigma, rate)
                + bs_price(PUT, spot, strike, t_years, sigma, rate))
    d1, d2 = _d1_d2(spot, strike, t_years, sigma, rate)
    discounted_strike = strike * math.exp(-rate * t_years)
    if kind == CALL:
        return spot * _norm_cdf(d1) - discounted_strike * _norm_cdf(d2)
    return discounted_strike * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


@dataclass(frozen=True, slots=True)
class Greeks:
    delta: float       # dV/dS
    gamma: float       # d2V/dS2
    vega: float        # dV/dsigma, per 1.00 of vol
    theta: float       # dV/dt, per year (negative for long premium)
    vanna: float       # d2V/dS dsigma -- how delta moves when vol moves
    volga: float       # d2V/dsigma2 -- convexity in vol

    @property
    def theta_per_day(self) -> float:
        return self.theta / 365.0


def bs_greeks(kind: str, spot: float, strike: float, t_years: float,
              sigma: float, rate: float = 0.0) -> Greeks:
    """Analytic greeks per unit of underlying, evaluated at the given state."""
    _validate(kind, spot, strike, t_years, sigma)
    if kind == STRADDLE:
        call = bs_greeks(CALL, spot, strike, t_years, sigma, rate)
        put = bs_greeks(PUT, spot, strike, t_years, sigma, rate)
        return Greeks(call.delta + put.delta, call.gamma + put.gamma,
                      call.vega + put.vega, call.theta + put.theta,
                      call.vanna + put.vanna, call.volga + put.volga)

    d1, d2 = _d1_d2(spot, strike, t_years, sigma, rate)
    sqrt_t = math.sqrt(t_years)
    pdf_d1 = _norm_pdf(d1)
    discounted_strike = strike * math.exp(-rate * t_years)

    gamma = pdf_d1 / (spot * sigma * sqrt_t)
    vega = spot * pdf_d1 * sqrt_t
    decay = -(spot * pdf_d1 * sigma) / (2.0 * sqrt_t)
    # Second-order cross terms. These are identical for a call and a put at
    # the same strike (they depend only on d1/d2/sigma), which is why a
    # straddle simply doubles them.
    vanna = -pdf_d1 * d2 / sigma
    volga = vega * d1 * d2 / sigma

    if kind == CALL:
        delta = _norm_cdf(d1)
        theta = decay - rate * discounted_strike * _norm_cdf(d2)
    else:
        delta = _norm_cdf(d1) - 1.0
        theta = decay + rate * discounted_strike * _norm_cdf(-d2)
    return Greeks(delta, gamma, vega, theta, vanna, volga)


def implied_vol(kind: str, price: float, spot: float, strike: float,
                t_years: float, rate: float = 0.0) -> float | None:
    """Invert a traded price into an implied volatility, or None.

    Bisection rather than Newton: it cannot diverge, and robustness matters
    more than the handful of iterations saved when the input is a real fill
    that may sit slightly outside the no-arbitrage band.  Returns None when
    no solution exists in the bracket (price below intrinsic or above the
    asymptote) -- callers must treat that as "unknown", never as zero.
    """
    if price <= 0 or spot <= 0 or strike <= 0 or t_years <= 0:
        return None
    try:
        low_price = bs_price(kind, spot, strike, t_years, _IV_LOW, rate)
        high_price = bs_price(kind, spot, strike, t_years, _IV_HIGH, rate)
    except (ValueError, OverflowError):
        return None
    if not (low_price <= price <= high_price):
        return None

    low, high = _IV_LOW, _IV_HIGH
    for _ in range(_IV_MAX_ITER):
        mid = 0.5 * (low + high)
        try:
            value = bs_price(kind, spot, strike, t_years, mid, rate)
        except (ValueError, OverflowError):
            return None
        if abs(value - price) < _IV_TOLERANCE or (high - low) < _IV_TOLERANCE:
            return mid
        if value < price:
            low = mid
        else:
            high = mid
    return 0.5 * (low + high)


@dataclass(frozen=True, slots=True)
class Observation:
    """One end of a round trip: what the market looked like at that moment."""
    underlying: float
    option_price: float
    t_years: float           # time to expiry remaining, in years


@dataclass(frozen=True, slots=True)
class Attribution:
    """Signed, position-scaled USD contributions. They sum to ``total``."""
    total: float
    delta: float
    gamma: float
    vega: float
    theta: float
    residual: float       # exactly -fees; spread hides in `vega`, see module doc

    entry_iv: float | None
    exit_iv: float | None
    reliable: bool
    notes: tuple[str, ...] = field(default=())

    @property
    def explained(self) -> float:
        """Everything the named risk factors account for."""
        return self.delta + self.gamma + self.vega + self.theta

    @property
    def residual_fraction(self) -> float:
        """Unexplained share of the move. With sequential revaluation there
        is no truncation error, so this is the fee load -- a cost
        measurement, not a modelling limitation."""
        scale = max(abs(self.total), abs(self.explained), 1e-9)
        return abs(self.residual) / scale

    def as_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "delta": self.delta,
            "gamma": self.gamma,
            "vega": self.vega,
            "theta": self.theta,
            "residual": self.residual,
            "entry_iv": self.entry_iv,
            "exit_iv": self.exit_iv,
            "reliable": self.reliable,
            "residual_fraction": self.residual_fraction,
            "notes": list(self.notes),
        }


def attribute_trade(*, kind: str, strike: float, entry: Observation,
                    exit: Observation, lots: float, contract_value: float,
                    direction: str, rate: float = 0.0,
                    fees_usd: float = 0.0) -> Attribution:
    """Decompose one closed round trip.

    ``direction`` is "LONG" or "SHORT" -- matching the Performance page's
    trade records.  A short position simply flips the sign of every
    contribution, so a short straddle that loses on a big move shows it as
    negative gamma, which is exactly the intuition we want surfaced.

    ``fees_usd`` is subtracted from ``total`` and folded into ``residual``,
    because fees are a real cost that no greek explains.  Pass the round-trip
    figure when you have it; the identity components-sum == total holds either
    way.
    """
    if direction not in ("LONG", "SHORT"):
        raise ValueError(f"direction must be LONG or SHORT, got {direction!r}")
    if lots <= 0 or contract_value <= 0:
        raise ValueError("lots and contract_value must be positive")

    sign = 1.0 if direction == "LONG" else -1.0
    scale = sign * lots * contract_value

    notes: list[str] = []

    # Realised P&L, defined identically to _reconstruct_trades_from_orders in
    # dashboard.py so the two never disagree.
    total = (exit.option_price - entry.option_price) * scale - fees_usd

    # Checked before the vol inversion, which would also fail at T=0 but
    # would report the less informative "could not invert" reason.
    if exit.t_years <= 0:
        # Held to expiry: there is no Black-Scholes state to walk to, and
        # settlement into intrinsic is not a vol or theta story.
        notes.append("position closed at/after expiry; not decomposed")
        return Attribution(total=total, delta=0.0, gamma=0.0, vega=0.0,
                           theta=0.0, residual=total, entry_iv=None,
                           exit_iv=None, reliable=False, notes=tuple(notes))

    entry_iv = implied_vol(kind, entry.option_price, entry.underlying,
                           strike, entry.t_years, rate)
    exit_iv = implied_vol(kind, exit.option_price, exit.underlying,
                          strike, exit.t_years, rate)

    if entry_iv is None or exit_iv is None:
        # Without an entry IV there are no greeks at all, so nothing can be
        # attributed. Report the total honestly and put all of it in residual
        # rather than inventing a decomposition.
        notes.append("implied vol could not be inverted; no greeks available")
        return Attribution(total=total, delta=0.0, gamma=0.0, vega=0.0,
                           theta=0.0, residual=total,
                           entry_iv=entry_iv, exit_iv=exit_iv, reliable=False,
                           notes=tuple(notes))

    # Sequential revaluation ("P&L explain"), not a Taylor expansion: walk
    # the position from entry state to exit state one variable at a time and
    # fully reprice at each step. The steps telescope, so they sum to the
    # model price change *exactly* -- there is no truncation error, which a
    # second-order expansion cannot promise (adding a correct vanna/volga
    # term was measured making the fit worse on a 3% move with a 30% IV
    # crush, because the third-order terms it exposed were larger still).
    #
    # Order is time -> spot -> vol. Any sequential attribution is
    # order-dependent: the spot/vol interaction is credited to whichever
    # step runs second. Vol last means the vol term is measured at the
    # *realised* spot, which is the reading we want when asking "did IV
    # move against the position after it was right on direction?".
    def value(spot: float, sigma: float, t_years: float) -> float:
        return bs_price(kind, spot, strike, t_years, sigma, rate)

    at_entry = value(entry.underlying, entry_iv, entry.t_years)
    after_time = value(entry.underlying, entry_iv, exit.t_years)
    after_spot = value(exit.underlying, entry_iv, exit.t_years)
    at_exit = value(exit.underlying, exit_iv, exit.t_years)

    theta_pnl = (after_time - at_entry) * scale
    spot_pnl = (after_spot - after_time) * scale
    vega_pnl = (at_exit - after_spot) * scale

    # Split the (exact) spot step into its linear and curvature parts, so a
    # short straddle's negative convexity is still visible as its own line.
    # Delta is taken after the time step, matching where the spot walk starts.
    d_spot = exit.underlying - entry.underlying
    delta_at_step = bs_greeks(kind, entry.underlying, strike, exit.t_years,
                              entry_iv, rate).delta
    delta_pnl = delta_at_step * d_spot * scale
    gamma_pnl = spot_pnl - delta_pnl

    # Because entry_iv/exit_iv are inverted *from the traded prices*, the
    # revaluation reproduces both ends exactly, so whatever is left is real:
    # fees, and any inconsistency between the two marks. It is not a
    # modelling artefact.
    residual = total - (delta_pnl + gamma_pnl + vega_pnl + theta_pnl)

    reliable = True
    # Deep ITM/OTM: price barely responds to vol, so the inverted IV (and
    # therefore the split between the vol step and the rest) is noise-driven
    # even though the steps still sum correctly.
    if bs_greeks(kind, entry.underlying, strike, entry.t_years,
                 entry_iv, rate).vega < _MIN_VEGA_FOR_IV:
        reliable = False
        notes.append("vega near zero; implied vol and vega term are unstable")
    if exit.t_years > entry.t_years:
        reliable = False
        notes.append("exit expiry later than entry: check inputs")

    result = Attribution(total=total, delta=delta_pnl, gamma=gamma_pnl,
                         vega=vega_pnl, theta=theta_pnl, residual=residual,
                         entry_iv=entry_iv, exit_iv=exit_iv,
                         reliable=reliable, notes=tuple(notes))
    # Fees aside, an unexplained remainder means the two marks are not
    # mutually consistent -- a stale or crossed print, most often.
    if abs(residual) - abs(fees_usd) > 1e-6 and result.residual_fraction > _MAX_RESIDUAL_FRACTION:
        notes.append("unexplained P&L exceeds the whole move; the entry or "
                     "exit mark looks stale or off-market")
        result = Attribution(total=total, delta=delta_pnl, gamma=gamma_pnl,
                             vega=vega_pnl, theta=theta_pnl,
                             residual=residual, entry_iv=entry_iv,
                             exit_iv=exit_iv, reliable=False,
                             notes=tuple(notes))
    return result


def summarise(attributions: list[Attribution]) -> dict[str, object]:
    """Aggregate a set of trades into the per-leg totals worth acting on.

    This is the number that answers "which leg is bleeding": if ``theta``
    dominates the negative side, entries are fine but holds are too long; if
    ``vega`` does, the vol regime is wrong for the structure being traded.
    """
    if not attributions:
        return {"trades": 0, "total": 0.0, "delta": 0.0, "gamma": 0.0,
                "vega": 0.0, "theta": 0.0, "residual": 0.0,
                "reliable_trades": 0, "unreliable_trades": 0}
    return {
        "trades": len(attributions),
        "total": sum(a.total for a in attributions),
        "delta": sum(a.delta for a in attributions),
        "gamma": sum(a.gamma for a in attributions),
        "vega": sum(a.vega for a in attributions),
        "theta": sum(a.theta for a in attributions),
        "residual": sum(a.residual for a in attributions),
        "reliable_trades": sum(1 for a in attributions if a.reliable),
        "unreliable_trades": sum(1 for a in attributions if not a.reliable),
    }
