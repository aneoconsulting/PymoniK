# Pricing workflows

Two patterns for financial pricing on ArmoniK, side by side: a simple
synchronous workflow that prices one instrument at a time, and a
fan-out/fan-in workflow that decomposes complex pricing into a DAG of
subtasks. Both are runnable; the source lives at
`examples/pricing_workflows.py`.

## Why pricing on ArmoniK

Pricing financial instruments is a natural fit for ArmoniK:

- **Embarrassingly parallel** for portfolio pricing (every instrument
  is independent).
- **Heterogeneous task sizes** (a vanilla call is microseconds; a
  Bermudan swaption with Monte Carlo paths is seconds-to-minutes).
- **Bursty demand** (end-of-day risk runs, intraday what-if analysis,
  one-off scenario shocks).

PymoniK handles the scheduling; you write the pricing math.

## Scenario 1 — Single-instrument synchronous pricing

The simplest possible workflow: one task in, one price out.

```python
from pymonik import PymonikClient, task

@task
def price_option(market_data: dict, option_def: dict, params: dict) -> dict:
    """Black-Scholes price + greeks for a vanilla European option."""
    spot   = market_data["spot"]
    rate   = market_data["rate"]
    sigma  = market_data["volatility"]
    strike = option_def["strike"]
    tte    = option_def["time_to_expiry"]
    is_call = option_def["type"] == "call"

    # ... Black-Scholes math ...
    price = compute_bs_price(spot, strike, tte, rate, sigma, is_call)
    delta = compute_bs_delta(...)
    vega  = compute_bs_vega(...)

    return {
        "price": price,
        "greeks": {"delta": delta, "vega": vega},
        "valuation_id": params["valuation_id"],
    }


def run() -> dict:
    market = {"spot": 100.0, "rate": 0.05, "volatility": 0.2}
    option = {"type": "call", "strike": 105.0, "time_to_expiry": 0.5,
              "notional": 1_000_000}
    params = {"valuation_id": "single-001"}

    with PymonikClient() as client:
        with client.session(partition="pymonik") as s:
            result = price_option.spawn(market, option, params).result(timeout=30)
    return result
```

Three things worth noting:

- The function is plain Python — no special imports, no ArmoniK API
  surface inside the math. The decorator is the only PymoniK touch.
- All inputs and outputs are JSON-serialisable dicts. PymoniK doesn't
  require this — it cloudpickles whatever you pass — but it makes
  log/debug output legible.
- One submission, one wait. Suitable for "I want to price this and
  see the answer", interactive notebooks, UI-driven tools.

## Scenario 2 — Decomposed pricing with subtasking

For a complex instrument (Bermudan swaption, multi-asset basket,
exotic with path dependencies), you want to:

1. Decompose into independent sub-pricings.
2. Run them in parallel.
3. Aggregate the results.

```python
from pymonik import PymonikClient, task

@task
def price_leg(market_data: dict, leg_def: dict) -> dict:
    """Price one leg of a multi-leg structure."""
    # ... per-leg math ...
    return {"leg_id": leg_def["id"], "price": ..., "sensitivities": ...}


@task
def aggregate_legs(leg_results: list[dict], structure: dict) -> dict:
    """Combine leg prices into the structure's overall price."""
    total_price = sum(r["price"] * structure["weights"][r["leg_id"]] for r in leg_results)
    return {
        "structure_id": structure["id"],
        "price": total_price,
        "leg_breakdown": leg_results,
    }


@task
def shock_structure(structure: dict, market: dict, shock: dict) -> dict:
    """Re-price under a market shock (delta-hedge analysis, scenario PnL)."""
    shocked_market = apply_shock(market, shock)
    leg_futures = price_leg.starmap(
        (shocked_market, leg) for leg in structure["legs"]
    )
    return aggregate_legs.spawn(list(leg_futures), structure).result()
    # Note: this .result() is OK because shock_structure runs on the
    # client side (it's not @task-decorated to be remote, but if it
    # were, you'd return the future from aggregate_legs.spawn instead
    # — see the sub-task / delegate pattern below).


def run_structure(structure: dict, market: dict) -> dict:
    with PymonikClient() as client:
        with client.session(partition="pymonik") as s:
            leg_futures = price_leg.starmap(
                (market, leg) for leg in structure["legs"]
            )
            result = aggregate_legs.spawn(leg_futures, structure)
            return result.result(timeout=300)
```

What this gives you:

- `price_leg.starmap(...)` submits every leg in one gRPC call.
  ArmoniK schedules them in parallel. We use `starmap` because each
  leg's args come from a different shape (the market is a constant,
  the leg varies); `map(*iterables)` would need parallel iterables of
  the same length.
- `aggregate_legs.spawn(leg_futures, structure)` doesn't wait for the
  legs on the client. PymoniK rewrites each upstream future as a
  `data_dependency`, ArmoniK schedules `aggregate_legs` to run *after*
  all legs complete, and `aggregate_legs` receives the resolved list
  of leg results.
- Only the terminal `.result(timeout=300)` blocks the client.

## Scenario 2b — Sub-tasking from inside a worker

When the decomposition itself depends on the input (e.g. a basket
whose number of underlyings isn't known until you parse the trade),
let the worker do it:

```python
@task
def price_basket(market: dict, basket: dict) -> dict:
    """Price every underlying, then aggregate. The fan-out happens
    inside the worker because we don't know `basket["underlyings"]`
    until we've parsed the trade.
    """
    underlying_results = price_leg.starmap(
        (market, u) for u in basket["underlyings"]
    )
    # Hand off our expected output to the aggregator subtask.
    return aggregate_legs.spawn(
        underlying_results, basket, _delegate=True
    )
```

The `_delegate=True` flag retargets the child task's
`expected_output_ids` to the parent's. ArmoniK delivers the child's
result to the original awaiter; the parent's own return value is
ignored (it returns a `Future`, which the SDK detects as a tail
call).

This is the right pattern for divide-and-conquer when the shape of
the recursion depends on the input.

## Putting it together: a portfolio run

End-of-day risk runs typically price thousands of instruments under
hundreds of market scenarios. PymoniK turns this into:

```python
def run_portfolio(portfolio: list[dict], scenarios: list[dict],
                  market: dict) -> list[dict]:
    with PymonikClient() as client:
        with client.session(partition="pymonik") as s:
            jobs = []
            for scenario in scenarios:
                shocked = apply_shock(market, scenario)
                for trade in portfolio:
                    jobs.append((shocked, trade, {"scenario_id": scenario["id"]}))

            futures = price_option.starmap(jobs)
            return futures.results(timeout=3600)
```

For 10,000 trades × 100 scenarios = 1M tasks, this submits in
batched RPCs (32-task batches by default), schedules across however
many workers ArmoniK can give you, and streams results back via the
events stream. Add `events=True` (default) and you'll see results
resolve as they arrive, not all at once at the end.

## Operational notes

- **Use blobs for shared market data.** If `market` is large
  (full vol surfaces, yield curves), upload it once with
  `blob.upload(market)` and pass the blob handle to every task
  instead of inlining. See
  [Blobs and Materialize](../guides/blobs-and-materialize.md).
- **Use multi-partition for heterogeneous compute.** Vanilla pricing
  on a CPU partition; Monte Carlo on a GPU partition.
  `client.session(partition=["cpu", "gpu"])` plus
  `monte_carlo.with_options(partition="gpu")` per task.
- **Trace it.** Pricing pipelines are exactly the workload OTel was
  designed for: a long DAG, occasional slow tasks, fan-in steps that
  depend on a thousand upstreams. See
  [Observability](../guides/observability.md).
- **Cache for what-if iterations.** Re-running the same pricing with
  the same inputs is wasteful. `PymonikClient(cache=True)` and
  `@task(cache=True)` short-circuit identical re-submissions.
