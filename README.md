# option-contract-grader

**Every option contract in the chain, scored 0-100 and graded A through F.**

![License](https://img.shields.io/badge/license-MIT-blue?style=flat-square)
![Tests](https://img.shields.io/badge/tests-49%20passing%2C%20offline-brightgreen?style=flat-square)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-003B57?style=flat-square&logo=sqlite&logoColor=white)
![Frontend](https://img.shields.io/badge/frontend-no%20build%20step-orange?style=flat-square)
![Deps](https://img.shields.io/badge/python%20deps-6-lightgrey?style=flat-square)

A local FastAPI app that pulls real US equity option chains, re-derives implied volatility and the Greeks itself with Black-Scholes-Merton, and hands back every contract scored 0-100 with a letter grade. It runs for a single ticker, or it sweeps the whole optionable universe (roughly 6,000 names) and returns a ranked board of the best contracts it found.

This is the calculating companion to [**shadow-options-trading-lab**](https://github.com/csnyder256/shadow-options-trading-lab). The lab runs strategies. This repo does the option math and the scoring. They are separate programs by design: no shared code, no shared database.

---

## What it actually does

1. Fetches the option chain for a ticker (or for every name in the universe) from CBOE's free delayed feed or from Tradier.
2. Throws away the vendor's IV and Greeks, and solves for implied volatility from each contract's own mid price.
3. Scores each contract on seven independent dimensions, blends them into one weighted composite, and assigns a letter.
4. Sorts, applies a per-underlying board cap, and returns the top contracts with a plain-English explanation attached to every sub-score.

Scope: long single-leg positions only, buying calls and puts. No spreads, no selling premium. The config file describes itself as buyer-tuned.

### The problem it solves

The vendor's numbers are not the numbers you are trading against. Tradier's IV and Greeks come from a third-party model refreshed roughly hourly, and CBOE's are a delayed snapshot. Both drift against the mid price you would actually pay. And nobody sells you historical implied volatility for free, so IV rank, the single most useful "is this expensive right now" signal, is not in either feed. This app computes the first from the quote and bootstraps the second from its own daily snapshots.

---

## Why it does its own math

The solver in `app/engine/blackscholes.py` is not a naive Newton loop:

- It checks the no-arbitrage bounds first. Below discounted intrinsic, or above discounted spot (calls) / discounted strike (puts), there is no valid sigma and it returns `None`.
- It seeds with the Brenner-Subrahmanyam ATM approximation instead of a blind 0.20.
- It runs Newton-Raphson on Vega, and bails out to a 200-iteration bisection over `[1e-4, 5.0]` the moment Vega drops under `1e-10` or sigma leaves the sensible region.
- If the bracket has no sign change, it returns `None` rather than a spurious root. A garbage IV would poison four of the seven sub-scores, so no answer beats a wrong one.

When the solve fails the contract is not silently dropped and it is not silently faked. It falls back to the vendor IV and carries the flag `Using vendor IV (couldn't solve from price)`. If there is no clean IV at all, the flag reads `No clean IV (illiquid / one-sided market)`. That fallback path is why "does not trust vendor Greeks" is a description of behavior rather than a slogan.

Two more things worth pointing at:

**Greeks come back in chain-quote conventions, not textbook units.** Theta is divided by 365 (per calendar day), vega and rho by 100 (per one percentage point). The conventions are documented in the module docstring and pinned by `test_greek_signs` and `test_put_call_parity`, because a factor-of-365 mismatch is the kind of bug that silently produces plausible-looking nonsense.

**Realized volatility is Yang-Zhang, not close-to-close.** It combines the overnight jump, the open-to-close move, and a Rogers-Satchell drift-independent term, with an automatic fallback to close-to-close when the estimator yields a non-positive variance. This matters because the highest-weighted sub-score (Value, weight 30) prices the contract at realized vol and compares that to the market mid. A bad HV estimate moves the top of the board.

---

## The grade

Seven independent 0-100 sub-scores blend into one weighted composite. Weights live in `app/config.py` as `DEFAULT_WEIGHTS` and sum to 100.

| Sub-score | Weight | What it measures |
|---|---:|---|
| Value (Price Edge) | 30 | BSM fair value computed at *realized* vol vs. the actual market mid |
| Odds of Profit | 20 | `N(d2)` evaluated at the break-even price |
| Liquidity | 20 | `0.6 * spread + 0.25 * log-scaled OI + 0.15 * log-scaled volume` |
| Volatility Value | 12 | IV/HV ratio, blended 60/40 with an inverted IV rank when rank exists |
| Move Needed | 10 | Required break-even move divided by the 1-standard-deviation move |
| Time-Decay Risk | 4 | Theta as a fraction of premium per day, halved under 7 DTE |
| Leverage & Risk | 4 | Gaussian bump, `exp(-((abs(delta) - 0.45) / 0.30)^2)` |

Grade bands come from `GRADE_BANDS` in `app/config.py`. The wording below paraphrases the app's own copy, which is blunter than this table.

| Grade | Score | Meaning |
|---|---:|---|
| A | 90-100 | Odds, price, liquidity and math all line up |
| B | 75-89 | Strong setup, a couple of things are not perfect |
| C | 60-74 | Playable but mediocre. No real edge either way |
| D | 40-59 | Weak. The price or the odds are working against you |
| F | 0-39 | Overpriced, illiquid, or the odds are ugly |

**The liquidity gate is a hard cap, not a penalty.** If the bid/ask spread exceeds 15% of mid, or open interest is under 50, the composite is clamped to 39.0 (grade F) no matter how good the other six look. The reasoning is in the code: if the quote is that wide, the mid is fiction, so the IV solved from it is fiction, so every downstream number is fiction.

Six human-readable flags can be attached to a contract, all in `app/engine/scoring.py`:

- `Expires today (0DTE) - extreme gamma risk`
- `No price (no bid/ask/last)`
- `Using vendor IV (couldn't solve from price)`
- `No clean IV (illiquid / one-sided market)`
- `Under 7 DTE - decay & gamma risk are high`
- `Illiquid - overall capped; other scores unreliable`

Every sub-score also carries a plain-English `explanation` string and a raw `metric` string, both rendered in the UI under a "Why this grade?" disclosure. You read one letter instead of interpreting delta against theta against open interest.

Scoring is deterministic. Same chain in, same grades out. Nothing is sampled, fitted, or trained.

---

## Architecture

Dependency flow is one-way: frontend to `api.py`, `api.py` to `scanner.py` and `market.py`, both of those down into `engine/` and `providers/`, with `store.py` as the only stateful layer.

`app/scanner.py` holds the per-symbol core used by **both** the single-ticker endpoint and the market sweep: expiration selection, optional ATM strike pruning, the ATM IV solve and snapshot, the premium pre-filter, scoring, sort. It is deliberately free of FastAPI types so `market.py` can call it without a circular import.

| File | LOC | Role |
|---|---:|---|
| `app/api.py` | 262 | FastAPI surface. Providers and the store are lazily-created singletons, so importing the app never requires a token. `StaticFiles` is mounted last so API routes win. |
| `app/config.py` | 153 | One dataclass, exactly 25 env-var knobs: weights, grade bands, liquidity thresholds, sweep tuning. No threshold is hardcoded in logic. |
| `app/models.py` | 142 | Dataclasses. `Contract` derives `mid` / `has_two_sided_market` / `spread_pct`, falling back to last trade on a one-sided market. |
| `app/engine/blackscholes.py` | 215 | BSM with continuous dividend yield `q`: `bs_price`, `greeks`, `implied_vol`, `prob_itm`. |
| `app/engine/volatility.py` | 150 | Close-to-close and Yang-Zhang realized vol, IV/HV ratio, `iv_rank`, `iv_percentile`. |
| `app/engine/scoring.py` | 270 | The seven sub-scores, the composite, the liquidity gate, the English strings. |
| `app/engine/grading.py` | 41 | Score to letter plus meaning, and the `grade_key()` the UI legend reads. |
| `app/providers/base.py` | 92 | Abstract `OptionsDataProvider` (four required methods) plus `FeedError` and two optional-optimization hooks with working defaults. |
| `app/providers/cboe.py` | 332 | Pure parser functions plus a thin networked class. OCC symbol parsing, symbol-spelling fallbacks, in-process chain cache. |
| `app/providers/tradier.py` | 304 | Same pure-parser / client split, but a different retry policy (429 only). Batched quotes, scalar-vs-array normalization, client-side rate limiting at 118 requests/minute against a 120 cap. |
| `app/scanner.py` | 131 | The shared per-symbol core described above. |
| `app/market.py` | 373 | Universe loading, the locked background state machine, and `run_market_sweep` as a testable function taking a progress callback and injectable providers. |
| `app/store.py` | 164 | SQLite: `iv_snapshots` and `underlying_cache`, an in-place `ALTER TABLE` migration, `check_same_thread=False` plus an `RLock` so sweep workers share one connection. |
| `scripts/update_universe.py` | 172 | Regenerates the optionable universe from the OCC directory, with fallbacks. |
| `frontend/` | 558 | Two tabs, live progress bar, client-side re-sort by any sub-score. Zero JS dependencies. |

Totals: 23 Python files, 3,465 lines (2,632 under `app/`, 661 under `tests/`, 172 in `scripts/`). Six dependencies in `requirements.txt`: five runtime (fastapi, uvicorn, httpx, pydantic, python-dotenv) and pytest.

### Data backends

Both sit behind the same four-method interface, so a third one is a drop-in. `app/providers/base.py` names Robinhood and ThetaData as the intended next backends.

| | CBOE (default) | Tradier |
|---|---|---|
| Credentials | none | brokerage API token |
| Freshness | roughly 15 minutes delayed | real-time on production |
| Chain fetch | one bulk JSON payload per name | one REST call per expiration |
| History | Yahoo public chart endpoint | Tradier history endpoint |
| Batch quotes | falls back to a per-symbol loop | `POST /markets/quotes`, 100 symbols per request |

Robinhood was evaluated and set aside for now. `robin_stocks` issues one request per contract with no published rate limit, so refreshing a single chain means hundreds of requests per second, which is a documented way to get an account blocked. Tradier serves a whole expiration in one call under a 120 requests/minute cap.

The two live providers do not retry identically. CBOE retries with exponential backoff plus jitter on 403/429/5xx and raises a typed `FeedError` carrying the symbol, so failures get counted and reported instead of silently vanishing. Tradier retries on HTTP 429 only, honoring the server's `Retry-After` header when present and falling back to exponential backoff (no jitter) when it is not; every other error status goes straight to `raise_for_status()` and surfaces as `httpx.HTTPStatusError`. Bringing Tradier up to the CBOE contract is listed under "Status and known rough edges" below.

### HTTP surface

| Method | Path | Purpose |
|---|---|---|
| POST | `/scan` | One ticker plus filters, returns a graded, ranked list |
| POST | `/market/scan` | Starts a background sweep, returns immediately |
| GET | `/market/status` | Polls progress, phase, notes, and Top-N results |
| GET | `/health` | Config sanity, provider in use, universe size |
| GET | `/key` | Grade legend and sub-score labels |
| GET | `/` | The static frontend |

```bash
curl -s localhost:8000/scan \
  -H 'content-type: application/json' \
  -d '{"ticker":"AAPL","side":"calls","limit":10}' | python -m json.tool
```

---

## The market sweep

The sweep is the most engineered part of the repo. It is a cost-reduction funnel, not a loop over 6,000 tickers.

```
universe (liquidity-ordered, ~6,000 names)
        |
   [1] price + HV pre-pass
        |   SQLite TTL cache first (default 12h)
        |   then batched quotes (Tradier, 100/request)
        |   or a bounded thread pool of Yahoo chart calls
        |   (one call yields price AND history together)
        v
   [2] prune to the requested stock-price band
        |
        v
   [3] fetch + score chains for survivors only
        |   <= 300 in-band names and a token present -> real-time Tradier
        |   otherwise                                -> CBOE bulk delayed
        |   bounded by MAX_CHAIN_SCANS
        v
   [4] global rank, then a per-underlying board cap (default 2)
        v
       Top N + notes explaining exactly what was covered
```

Details that matter:

- **The CBOE provider overrides `get_price_and_history()`** so one small Yahoo chart call yields both the price and the OHLC bars. Without that override, stage 1 would download a roughly 1.5 MB option chain just to read a stock price.
- **Provider capability hooks have working defaults.** `get_quotes_batch()` defaults to a per-symbol loop for every provider; Tradier overrides it and sets `supports_batch_quotes = True`. No provider is *required* to implement it, but the one that can lets the sweep prune 6,000 names in a few dozen requests.
- **The per-underlying board cap** stops one hot name from filling the Top 50 with its own strike ladder. `test_board_has_many_distinct_names_not_a_few` asserts at least 15 distinct names across a 20-name universe. The test docstring names the bug it locks down.
- **Nothing fails silently.** The sweep accumulates `price_failed` and `chain_failed` lists and emits notes stating names priced vs. attempted, budget truncation (`Scanned the top N of M in-band names by liquidity`), chain-fetch failures with example symbols, the routing mode actually used, and a data-as-of timestamp. Two tests assert those notes contain what they claim.

### IV Rank is bootstrapped from nothing

Neither CBOE nor Tradier serves historical implied volatility, so the app builds its own. Each scan solves the ATM IV for the symbol and snapshots it into SQLite, one row per symbol per day. `iv_rank()` returns `None` below 10 observations, at which point the Volatility sub-score falls back to pure IV-vs-HV and the API emits a note: `IV Rank is warming up (N day(s) of history; needs ~10)`. Rank gets better the longer you run it, and it is honest about not having it yet.

One bug fix worth preserving lives in `Store.get_underlying_fresh()`: cache freshness is keyed on an `updated_at` UTC timestamp, not on a calendar `snap_date`. A TTL spanning midnight therefore still finds yesterday's row. `test_underlying_cache_ttl_respects_age_and_ignores_snap_date` inserts a row stamped with yesterday's date but a one-hour-old timestamp and asserts it is still a cache hit. The docstring calls it the cross-midnight fix for the intermittent empty-board bug.

---

## Quickstart

No API key is needed. `DATA_PROVIDER=cboe` is the default and runs on the public delayed feed with zero credentials. Install into a virtualenv: the test suite needs `httpx` and collection fails without it.

```bash
git clone https://github.com/csnyder256/option-contract-grader
cd option-contract-grader

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env               # optional, the defaults work as-is
uvicorn app.api:app --reload
# open http://localhost:8000
```

### Windows, no terminal

1. Double-click `setup.bat` once. Creates `.venv`, installs requirements, copies `.env.example` to `.env`.
2. Double-click `start.bat`. Serves on `http://127.0.0.1:8000` and opens a browser after a few seconds. Leave the window open.
3. `stop.bat` force-kills the server if you closed the start window.

### Going real-time

Set these in `.env`:

```ini
DATA_PROVIDER=tradier
TRADIER_TOKEN=your-tradier-token-here
TRADIER_ENV=production      # "sandbox" is still ~15 min delayed
```

A free Tradier brokerage account is required for real-time option data. Bring your own token and comply with each vendor's data terms. This repo ships no credentials and no vendor data.

### Regenerating the universe

```bash
python scripts/update_universe.py
```

This writes `app/universe/optionable.txt` from the OCC Directory of Listed Products, falling back to the Cboe symbol reference and then to the curated list. It refuses to overwrite if it assembles fewer than 565 symbols or fewer than 400 S&P names, so a half-failed download cannot silently shrink the screener's coverage. Cash-settled index roots are dropped, symbols are deduped on a separator-stripped key, and the output is ordered ETFs, then S&P 500, then the alphabetical long tail. That ordering is what makes the sweep's scan budget mean "the most liquid slice."

---

## Configuration

All 25 knobs are environment variables read in `app/config.py` and documented in `.env.example`. The ones you are most likely to touch:

| Variable | Default | What it does |
|---|---|---|
| `DATA_PROVIDER` | `cboe` | `cboe` (free, delayed, no account) or `tradier` |
| `RISK_FREE_RATE` | `0.04` | The `r` fed to Black-Scholes-Merton |
| `HV_WINDOW` | `30` | Trading days of history in the Yang-Zhang estimate |
| `DEFAULT_MIN_DTE` | `14` | Lower end of the default expiration window |
| `DEFAULT_MAX_DTE` | `60` | Upper end of it |
| `MAX_SPREAD_PCT` | `0.15` | Spread above this trips the hard liquidity cap |
| `MIN_OPEN_INTEREST` | `50` | Open interest below this trips the same cap |
| `MAX_CHAIN_SCANS` | `2000` | Cap on in-band names a sweep fetches chains for (0 = no cap) |
| `REALTIME_CHAIN_THRESHOLD` | `300` | At or below this many in-band names, route chains to Tradier |
| `PER_NAME_BOARD_CAP` | `2` | Contracts allowed per underlying on the final board |
| `SWEEP_MAX_WORKERS` | `4` | Thread-pool width for the sweep's price pre-pass |
| `CACHE_TTL_HOURS` | `12.0` | How stale a cached price/HV row may be and still count |

---

## Testing

```bash
pytest -q
# 49 passed
```

40 test functions across five modules, 49 cases after parametrization (only `test_iv_roundtrip` is parametrized, 5 sigmas by 2 option types). Verified in a clean virtualenv on Python 3.14 while writing this: **49 passed in 2.14s**.

Everything runs offline. No network access, no mocking library, no recorded-cassette dependency. Every network parser is a pure module-level function that takes a dict, so the provider tests feed it literal payloads. The two retry-path tests monkeypatch a fake transport.

- `test_blackscholes.py`: BSM against textbook values, put-call parity, IV round-trip across several sigmas and both sides, IV below intrinsic returns `None`, Greek sign conventions, `prob_itm` monotonic in strike.
- `test_scoring.py`: all sub-scores bounded to `[0,100]`, Value rises with realized vol, the liquidity gate actually caps the composite, tighter spread scores more liquid, a no-price contract bottoms out at F.
- `test_cboe.py`: OCC symbol parsing, quote/expiration/chain parsing with side and expiration filters, Yahoo history dropping nulls and sorting ascending, retry-then-succeed, `FeedError` after exhausting retries, multi-spelling symbol fallback.
- `test_tradier.py`: scalar-vs-array collapse normalization, all four parsers, empty payloads are safe, batch-quote keying and chunking, the combined price+history path.
- `test_market.py`: universe parsing, store round-trip, price-band pruning, descending sort, the distinct-names board cap regression, chain-failure counting surfaced in notes, budget truncation wording, order-independence (no alphabet bias), the cross-midnight TTL fix, and an end-to-end background sweep polled to completion through the real state machine.

Roughly a 1:4 test-to-application line ratio.

**Gaps, named honestly:** there are no HTTP-level tests of the five FastAPI endpoints (no `TestClient`), and no tests of the frontend JavaScript.

---

## What is not in this public copy

This repo is a sanitized copy of a working local install. The exclusions are enforced by `.gitignore`, not by hand: it has labeled sections for secrets, Python build output, runtime state, and the generated universe.

- **`.env`**, which held a live brokerage API token. `.gitignore` excludes it; `.env.example` ships instead with placeholder values and a comment on every knob.
- **`.venv/`** and all `__pycache__/`. Reproduced by `requirements.txt`.
- **`data/options.db`**, the accumulated SQLite state (IV snapshots plus the price/HV cache). Only public market data, no account or position information, but it is runtime output. `data/.gitkeep` ships so the path exists; the schema is created on first run.
- **`app/universe/optionable.txt`**, the roughly 6,000-name generated list. The OCC and Cboe source files carry redistribution restrictions, which `scripts/update_universe.py` acknowledges in its own docstring, so the generator ships and the generated artifact does not.

**That last one changes behavior on a fresh clone.** `app/market.py` prefers `optionable.txt` and falls back to `app/universe/sp500_etfs.txt`, a curated 565-symbol list (62 ETFs plus 503 S&P names). So out of the box the market sweep covers 565 names. Run `python scripts/update_universe.py` once to get the full optionable universe. `GET /health` reports `universe_size` so you can tell which list is loaded.

---

## Status and known rough edges

Working and used. The version string in `app/api.py` is `0.2.0`. Honest caveats:

- **No packaging and no CI.** There is no `pyproject.toml`, no `setup.py`, no lockfile, and no `.github/` directory. The test number above is one run locally in a clean venv, not one a service verified.
- **BSM is European; US equity options are American.** Inverting a European model against an American premium is an approximation. It is a good one for the non-dividend, non-deep-ITM majority of the board, and it is wrong at the edges. Early exercise is not modeled.
- **The free feeds are unofficial.** The CBOE delayed-quote CDN and the Yahoo chart endpoint are public but undocumented, and they can change or start rate-limiting without notice. That is why the retry, the backoff, and the failure counting exist.
- **Tradier sandbox is still delayed** and serves no Greeks. Only `TRADIER_ENV=production` on a brokerage account is real-time.
- **"Top 50 across the market" is bounded by the scan budget.** With `MAX_CHAIN_SCANS` set, it is the top 50 across the most-liquid slice actually scanned. The notes always say which.
- **Stale copy in two places.** The frontend tab still reads "Market (S&P 500 + ETFs)" (`frontend/index.html:18`) and the `app/market.py` module docstring still says "curated universe," both left over from before the OCC universe landed. The behavior is correct; the labels are behind.
- **The two providers do not fail the same way.** `cboe.py` retries 403/429/5xx with jittered backoff and raises `FeedError`; `tradier.py` retries 429 only and lets everything else surface as `httpx.HTTPStatusError`. Callers that catch `FeedError` will not catch a Tradier 5xx. Unifying the two is on the roadmap.
- **Config drift.** `.env.example` ships `MAX_CHAIN_SCANS=1500` while the built-in default in `config.py` is `2000`. The example is the lighter, faster setting.
- **This is a calculator, not a broker.** It places no orders and connects to no execution venue. A letter grade summarizes seven measurable properties of a contract at a point in time; it is not advice and none of the output should be read as a recommendation.

Roadmap, in rough priority order: `TestClient` coverage of the five endpoints, an American-option pricer (binomial or Bjerksund-Stensland) for the early-exercise cases, and persisting sweep results so a completed board survives a restart.

---

## Related

[**shadow-options-trading-lab**](https://github.com/csnyder256/shadow-options-trading-lab), the strategy side of the pair.

## License

MIT. See [LICENSE](LICENSE). Market data belongs to CBOE, Yahoo, Tradier, and the OCC respectively, and complying with their terms is on you. The `app/universe/sp500_etfs.txt` fallback is a small static set of ticker symbols compiled from public S&P 500 constituent listings.

Built by Cade (https://github.com/csnyder256)
