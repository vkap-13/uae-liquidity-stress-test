#!/usr/bin/env python3
"""
Monte Carlo Liquidity Stress Testing of a stylised UAE bank, calibrated on aggregate
CBUAE monthly bank-deposit statistics.

ONE command does everything:
    python src/liquidity_stress.py --csv data/cbuae_deposits.csv

  1. loads the CSV/XLSX and runs a DATA-QUALITY GATE (stops on probable typos)
  2. simulates 30-day deposit outflows under 5 stress scenarios x 2 calibrations
  3. writes results/*.csv, figures/*.png, report/research_report.md and README.md

The raw deposit series is NEVER written to any output, so the repository can be published
without redistributing the source data.

Input: two columns, Date and Deposits (any consistent unit), one row per month.
Dates may be Jan-21, Jan 2021, 2021-01-31 or 31/01/2021 (use --dayfirst for the last).

Other modes
    python src/liquidity_stress.py --check data/cbuae_deposits.csv   # data check only
    python src/liquidity_stress.py --demo                            # synthetic test run
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import date

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.stats import norm

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---- Hypothetical stress ladder: ASSUMPTIONS, not data. Edit and justify in the report.
# vol_mult    multiplies the historical deviation of monthly deposit growth
# runoff_mean mean extra 30-day run-off (share of deposits); Beta-distributed, sd = RUNOFF_CV x mean
SCENARIOS = {
    "Baseline": dict(vol_mult=1.0, runoff_mean=0.00),
    "Mild":     dict(vol_mult=1.5, runoff_mean=0.03),
    "Moderate": dict(vol_mult=2.0, runoff_mean=0.07),
    "Severe":   dict(vol_mult=3.0, runoff_mean=0.15),
    "Extreme":  dict(vol_mult=3.0, runoff_mean=0.25),
}
RUNOFF_CV = 0.4
MODELS = ("Normal", "Bootstrap")
RISK_THRESHOLD_PCT = 1.0      # acceptable exhaustion probability used in the write-up
MAX_MONTHLY_MOVE_PCT = 8.0    # data gate default (monthly); quarterly uses 12%

# Frequency settings. Quarterly growth is converted to a 30-day equivalent assuming independent
# months: mean / 3 and volatility / sqrt(3).
FREQS = {
    "monthly":   dict(unit="month", alias="ME", months=1, gate=8.0, min_obs=24, warn_below=60),
    "quarterly": dict(unit="quarter", alias="QE-DEC", months=3, gate=12.0, min_obs=12, warn_below=20),
}
P = dict(FREQS["monthly"])


# =============================================================== data loading
def parse_dates(col, dayfirst=False):
    col = col.astype(str).str.strip()
    for fmt in ("%b-%y", "%b %y", "%b-%Y", "%b %Y", "%B %Y", "%B-%Y", "%B %y"):
        t = pd.to_datetime(col, format=fmt, errors="coerce")
        if t.notna().all():
            return t + pd.offsets.MonthEnd(0)
    t = pd.to_datetime(col, dayfirst=dayfirst, errors="coerce", format="mixed")
    if t.notna().any() and (t.dropna().dt.year < 1990).any():
        raise SystemExit("ERROR: some dates parsed to years before 1990. Write dates as YYYY-MM-DD.")
    return t + pd.offsets.MonthEnd(0)


def load_deposits(path, dayfirst=False):
    if not os.path.exists(path):
        raise SystemExit(f"ERROR: file not found: {path}")
    df = pd.read_excel(path) if path.lower().endswith((".xlsx", ".xls")) else pd.read_csv(path)
    df.columns = [str(c).strip() for c in df.columns]
    date_col = next((c for c in df.columns if any(k in c.lower() for k in ("date", "month", "period"))), None)
    dep_col = next((c for c in df.columns if "deposit" in c.lower()), None)
    if dep_col is None and date_col is not None and len(df.columns) == 2:
        dep_col = [c for c in df.columns if c != date_col][0]
    if date_col is None or dep_col is None:
        raise SystemExit(f"ERROR: need a 'Date' column and a 'Deposits' column; found {list(df.columns)}")
    out = pd.DataFrame({"Date": parse_dates(df[date_col], dayfirst), "Deposits": df[dep_col]})
    if not pd.api.types.is_numeric_dtype(out["Deposits"]):
        out["Deposits"] = out["Deposits"].astype(str).str.replace(",", "", regex=False).str.strip()
    out["Deposits"] = pd.to_numeric(out["Deposits"], errors="coerce")
    bad = out[out.isna().any(axis=1)]
    if len(bad):
        print(f"[data] dropping {len(bad)} unparseable row(s) at file rows: "
              f"{[int(i) + 2 for i in bad.index][:10]}")
        out = out.dropna()
    dup = out["Date"].duplicated(keep="last")
    if dup.any():
        print(f"[data] dropping {int(dup.sum())} duplicate {P['unit']}(s): "
              f"{sorted(out.Date[dup].dt.strftime('%Y-%m').tolist())}")
        out = out[~dup]
    if (out["Deposits"] <= 0).any():
        raise SystemExit("ERROR: zero or negative deposit values found.")
    out = out.sort_values("Date").reset_index(drop=True)
    if len(out) < P["min_obs"]:
        raise SystemExit(f"ERROR: only {len(out)} {P['unit']}s; need at least {P['min_obs']}.")
    return out


def demo_data(n=120, seed=0):
    """Synthetic monthly deposits. NOT CBUAE data."""
    rng = np.random.default_rng(seed)
    g = 0.006 + 0.012 * rng.standard_t(4, n) * np.sqrt(2 / 4)
    return pd.DataFrame({"Date": pd.date_range("2016-01-31", periods=n, freq="ME"),
                         "Deposits": 1_500_000 * np.cumprod(1 + g)})


# =========================================================== data-quality gate
def quality_report(df, limit=None):
    """Return (list of issue dicts, list of warning strings)."""
    limit = P["gate"] if limit is None else limit
    u = P["unit"]
    v = df["Deposits"].values
    d = df["Date"]
    n = len(v)
    chg = np.r_[np.nan, 100 * (v[1:] / v[:-1] - 1)]
    issues, warns = [], []

    expected = pd.date_range(d.iloc[0], d.iloc[-1], freq=P["alias"])
    missing = sorted(set(expected) - set(d))
    if missing:
        warns.append(f"{len(missing)} missing {u}(s): "
                     f"{[m.strftime('%Y-%m') for m in missing][:12]}")

    for i in range(1, n):
        if abs(chg[i]) < limit:
            continue
        prev, cur = v[i - 1], v[i]
        nxt = v[i + 1] if i < n - 1 else np.nan
        ratio = cur / prev
        if any(abs(np.log10(ratio) - k) < 0.05 for k in (-3, -2, -1, 1, 2, 3)):
            kind = f"UNIT/DECIMAL SLIP (about 10^k times the previous {u})"
        elif nxt == nxt and abs(nxt / prev - 1) * 100 < limit:
            kind = f"SPIKE THAT REVERSES -> this {u} is probably mistyped"
        elif i >= 2 and abs(cur / v[i - 2] - 1) * 100 < limit:
            kind = f"REBOUND -> the previous {u} is probably mistyped"
        else:
            kind = "LEVEL SHIFT -> mixed series / definition change / unit change?"
        issues.append({"month": d.iloc[i].strftime("%Y-%m"), "prev_value": prev, "value": cur,
                       "next_value": nxt, "change_pct": chg[i], "diagnosis": kind})

    years = (d.iloc[-1] - d.iloc[0]).days / 365.25
    cagr = ((v[-1] / v[0]) ** (1 / years) - 1) * 100 if years > 0 else np.nan
    if cagr == cagr and (cagr > 25 or cagr < -10):
        warns.append(f"first-to-last growth is {cagr:.1f}% a year - implausible for aggregate deposits; "
                     "check units and series definition.")
    if n < P["warn_below"]:
        warns.append(f"only {n} {u}s; tail estimates will be uncertain.")
    return issues, warns


def print_quality(issues, warns, limit):
    for w in warns:
        print(f"[check] WARNING: {w}")
    if not issues:
        print(f"[check] PASSED: no {P['unit']}ly move above {limit:g}%.")
        return
    u = P["unit"]
    print(f"\n[check] FAILED: {len(issues)} {u}ly move(s) above {limit:g}%. Aggregate bank deposits "
          f"rarely move this much in a {u}, so these are probably data-entry errors or the wrong series.")
    print(f"\n  {'period':<9}{'previous':>16}{'this ' + u:>16}{'next':>16}{'change':>9}  diagnosis")
    for r in issues:
        nxt = f"{r['next_value']:,.1f}" if r["next_value"] == r["next_value"] else "-"
        print(f"  {r['month']:<9}{r['prev_value']:>16,.1f}{r['value']:>16,.1f}{nxt:>16}"
              f"{r['change_pct']:>8.1f}%  {r['diagnosis']}")
    print(f"\n  HOW TO FIX: open the CBUAE bulletin for each {u} listed, compare the value in your "
          "file with the bulletin (same table, same unit), correct the CSV, and rerun.\n"
          "  If a move is genuinely in the bulletin, rerun with --allow-large-moves and explain it "
          "in the report.")


# ================================================================ simulation
def simulate_outflow(g, model, vol_mult, runoff_mean, n, rng, keep_drift):
    """Net 30-day outflow as a share of deposits (positive = deposits leave)."""
    mu = g.mean() if keep_drift else 0.0
    if model == "Normal":
        dev = rng.normal(0.0, g.std(ddof=1), n)
    else:
        dev = rng.choice(g - g.mean(), size=n, replace=True)
    growth = mu + vol_mult * dev
    runoff = 0.0
    if runoff_mean > 0:
        sd = RUNOFF_CV * runoff_mean
        kappa = runoff_mean * (1 - runoff_mean) / sd ** 2 - 1
        runoff = rng.beta(runoff_mean * kappa, (1 - runoff_mean) * kappa, n)
    return runoff - growth


def metrics(w, d0, l0):
    W = w * d0
    end = l0 - W
    n = len(w)
    p = float((end < 0).mean())
    q = lambda c: float(np.percentile(W, 100 * c))
    return {
        "prob_exhaust_pct": 100 * p,
        "mc_std_error_pct": 100 * np.sqrt(p * (1 - p) / n),
        "mean_outflow_pct": 100 * float(w.mean()),
        "p95_outflow_pct": 100 * float(np.percentile(w, 95)),
        "p99_outflow_pct": 100 * float(np.percentile(w, 99)),
        "es99_outflow_pct": 100 * float(w[W >= q(0.99)].mean()),
        "p05_end_liquidity_bn": float(np.percentile(end, 5)),
        "p01_end_liquidity_bn": float(np.percentile(end, 1)),
        "buffer95_bn": max(q(0.95), 0.0),
        "buffer99_bn": max(q(0.99), 0.0),
        "buffer999_bn": max(q(0.999), 0.0),
        "buffer99_pct_of_deposits": 100 * max(q(0.99), 0.0) / d0,
    }


# ================================================================== write-up
def fmt_p(p, n):
    return f"< {300 / n:.2f}" if p == 0 else f"{p:.2f}"


def ci_txt(row):
    """95% interval for an exhaustion probability; omitted when no exhaustion occurred."""
    if row["prob_exhaust_pct"] == 0:
        return ""
    return f" (±{1.96 * row['mc_std_error_pct']:.2f})"


def build_texts(ctx):
    s, a, boot, nrm, conv, grid = ctx["s"], ctx["a"], ctx["boot"], ctx["nrm"], ctx["conv"], ctx["grid"]
    order = list(SCENARIOS)
    liq_pct = 100 * a.liquidity0 / a.deposits0
    n = a.paths

    safe = [x for x in order if boot.loc[x, "prob_exhaust_pct"] <= RISK_THRESHOLD_PCT]
    fail = [x for x in order if boot.loc[x, "prob_exhaust_pct"] > RISK_THRESHOLD_PCT]
    if fail and safe:
        f0 = fail[0]
        breaking = (f"Liquidity is resilient (exhaustion probability at or below {RISK_THRESHOLD_PCT:g}%) up to the "
                    f"**{safe[-1]}** scenario and becomes vulnerable under **{f0}**, where it is exhausted in "
                    f"{boot.loc[f0, 'prob_exhaust_pct']:.1f}% of simulated paths.")
        head = (f"the stylised bank withstands stress up to the {safe[-1]} scenario but is exhausted in "
                f"{boot.loc[f0, 'prob_exhaust_pct']:.1f}% of {f0} paths")
    elif not fail:
        breaking = (f"Exhaustion probability stays at or below {RISK_THRESHOLD_PCT:g}% in every scenario, "
                    "including Extreme; a harsher scenario is needed to find the breaking point.")
        head = "the stylised bank withstands every scenario tested"
    else:
        breaking = (f"Exhaustion probability exceeds {RISK_THRESHOLD_PCT:g}% in every scenario, including the "
                    "baseline - check the liquidity assumption and the data.")
        head = "the stylised bank is vulnerable even in the baseline"

    def min_ratio(sc):
        row = grid.loc[sc]
        ok = [c for c in row.index if row[c] <= RISK_THRESHOLD_PCT]
        return ok[0] if ok else f"> {row.index[-1]}"

    zs = []
    for x in order:
        se = np.hypot(nrm.loc[x, "mc_std_error_pct"], boot.loc[x, "mc_std_error_pct"])
        d = abs(nrm.loc[x, "prob_exhaust_pct"] - boot.loc[x, "prob_exhaust_pct"])
        zs.append(0.0 if d == 0 else (d / se if se > 0 else np.inf))
    if max(zs) < 2:
        model_txt = ("Normal and bootstrap calibrations give statistically indistinguishable exhaustion "
                     "probabilities in every scenario (differences within two combined standard errors). "
                     "Because ordinary deposit volatility is small relative to the hypothetical run-offs, the "
                     "scenario assumptions, not the shape of the historical distribution, drive the results.")
    else:
        hi = "bootstrap" if (boot.prob_exhaust_pct - nrm.prob_exhaust_pct).mean() > 0 else "normal"
        model_txt = (f"The calibrations differ by more than two standard errors in at least one scenario; the "
                     f"{hi} calibration indicates higher risk on average, which points to the shape of the "
                     "historical distribution mattering.")

    c10, c50 = conv.set_index("paths").loc[10000], conv.set_index("paths").loc[50000]
    stable = abs(c10.prob_exhaust_pct - c50.prob_exhaust_pct) <= 2 * max(c10.mc_std_error_pct, 1e-9)
    conv_txt = (f"Under the Severe scenario the exhaustion probability is {c10.prob_exhaust_pct:.2f}% with "
                f"10,000 paths and {c50.prob_exhaust_pct:.2f}% with 50,000 paths "
                + ("- within sampling error, so 10,000 paths is sufficient."
                   if stable else "- a gap larger than two standard errors; more paths are advisable."))

    rows = "\n".join(
        f"| {x} | {fmt_p(boot.loc[x, 'prob_exhaust_pct'], n)}{ci_txt(boot.loc[x])} "
        f"| {fmt_p(nrm.loc[x, 'prob_exhaust_pct'], n)} | {boot.loc[x, 'p99_outflow_pct']:.1f} "
        f"| {boot.loc[x, 'es99_outflow_pct']:.1f} | {boot.loc[x, 'buffer99_pct_of_deposits']:.1f} "
        f"| {a.liquidity0 - boot.loc[x, 'buffer99_bn']:+.2f} |" for x in order)
    ladder = "\n".join(f"| {x} | {p['vol_mult']:g}x | {100 * p['runoff_mean']:.0f}% |" for x, p in SCENARIOS.items())
    need = "\n".join(f"| {x} | {min_ratio(x)} |" for x in order)
    sev = boot.loc["Severe"]

    demo_banner = ("> **DEMO RUN ON SYNTHETIC DATA - these numbers are not CBUAE results.**\n\n"
                   if ctx["demo"] else "")
    period = f"{s['first_date']} to {s['last_date']}"
    unit = P["unit"]
    freq_cap = s["frequency"].capitalize()
    gate = P["gate"]
    quarterly = P["months"] > 1
    quarterly_note = ("\n- **Frequency:** quarter-end values are used because Table 1 reports quarters for most of "
                      "the period. Quarterly growth is converted to a 30-day equivalent assuming independent months "
                      "(mean ÷ 3, standard deviation ÷ √3).") if quarterly else ""
    scale_note = " (quarterly deviations divided by √3)" if quarterly else ""
    scale_short = ", scaled from quarterly to 30 days" if quarterly else ""
    horizon_limit = ("Quarterly data are scaled to a 30-day horizon by assuming independent months, so "
                     "within-quarter swings, seasonality and intra-month timing are not captured") if quarterly else (
                     "Monthly data approximate a 30-day horizon; intra-month timing and seasonality are not captured")
    small_sample = (f" With only {int(s['n_changes'])} observations, tail estimates are uncertain; a monthly series "
                    "would strengthen them.") if quarterly else ""
    csv_name = ctx["csv_name"]
    freq_flag = " --frequency quarterly" if quarterly else ""

    report = f"""{demo_banner}# Monte Carlo Liquidity Stress Testing of the UAE Banking Sector

*{ctx['author']} · {date.today():%B %Y}*

## Abstract
{freq_cap} aggregate bank-deposit data from the Central Bank of the UAE ({period}, {int(s['n_changes'])} {unit}ly changes) are used to calibrate a Monte Carlo model of 30-day deposit outflows for a stylised bank holding AED {a.deposits0:g}bn of deposits and AED {a.liquidity0:g}bn ({liq_pct:.0f}%) of liquid assets. Across {n:,} simulated paths per scenario and five stress levels, {head}. Surviving 99% of Severe-scenario paths would require liquid assets of about {sev['buffer99_pct_of_deposits']:.1f}% of deposits.

## 1. Introduction
Liquidity risk is the risk that a bank cannot meet withdrawals as they fall due. Regulators address it through buffers such as the Basel III Liquidity Coverage Ratio, which requires banks to hold enough high-quality liquid assets to survive a 30-day stress. This project asks a simplified version of the same question: how likely is a stylised UAE bank to exhaust its liquid assets over 30 days, and how large a buffer would protect it under progressively harsher assumptions?

## 2. Data
- **Source:** Central Bank of the UAE, Monthly Statistical Bulletin (banking and monetary statistics). See `DATA.md` for the exact series and how to rebuild the input file.
- **Series:** "Deposits" (excluding inter-bank), Table 1 "Selected Monetary and Banking Indicators", AED million, {unit}-end values, {period}. Where a figure was revised in a later bulletin, the latest published value is used.
- **Processing:** {unit}-end dates; duplicates and unparseable rows removed; a data-quality check flags any {unit}ly move above {gate:g}% for verification against the source.{quarterly_note}
- **Redistribution:** the raw series is not included in this repository; only derived statistics are published.

| Statistic | Value |
|---|---|
| Mean {unit}ly growth | {s['mean_growth_pct']:.2f}% |
| Standard deviation ({unit}ly) | {s['sd_growth_pct']:.2f}% |
| Skewness | {s['skew']:.2f} |
| Excess kurtosis | {s['excess_kurtosis']:.2f} |
| Worst {unit} | {s['worst_period_growth_pct']:.2f}% ({s['worst_period']}) |
| 5th percentile {unit} | {s['p05_growth_pct']:.2f}% |
| 30-day equivalent mean / standard deviation | {s['mean_30day_equiv_pct']:.2f}% / {s['sd_30day_equiv_pct']:.2f}% |

![Historical deposit growth](../figures/historical_deposit_growth.png)

## 3. Methodology
For each scenario the 30-day net outflow, as a share of deposits, is

*w = R − (μ + k·ε)*

where *ε* is a 30-day-equivalent deviation of deposit growth{scale_note} drawn either from a normal distribution with the historical standard deviation (**Normal**) or by resampling historical deviations (**Bootstrap**); *k* scales volatility; *R* is an additional run-off drawn from a Beta distribution with the scenario's mean and a standard deviation equal to {RUNOFF_CV:.0%} of that mean; and *μ* is {'the historical mean growth' if a.keep_drift else 'set to zero, so organic deposit growth is not relied on (a conservative choice)'}.

Closing liquidity is *L_end = L₀ − w·D₀*. The bank is **exhausted** when *L_end < 0*. Each scenario uses {n:,} paths (seed {a.seed}).

**Stress ladder (hypothetical assumptions):**

| Scenario | Volatility multiplier | Mean extra run-off |
|---|---|---|
{ladder}

Run-off levels are illustrative and should be read alongside Basel III LCR run-off assumptions for different deposit types.

## 4. Results
| Scenario | P(exhaust) % bootstrap (95% CI) | P(exhaust) % normal | 99th pct outflow % | ES99 outflow % | 99% buffer, % of deposits | Surplus (+) / shortfall (−) vs held, AED bn |
|---|---|---|---|---|---|---|
{rows}

{breaking}

![Closing liquidity](../figures/closing_liquidity.png)

![Required buffer](../figures/required_buffer.png)

**Liquidity needed to keep exhaustion at or below {RISK_THRESHOLD_PCT:g}%** (bootstrap; grid of 10–35% of deposits):

| Scenario | Minimum liquid assets / deposits |
|---|---|
{need}

![Sensitivity heatmap](../figures/sensitivity_heatmap.png)

## 5. Validation
- **Model comparison:** {model_txt}
- **Monte Carlo error:** {conv_txt}
- **Zero probabilities** are reported as an upper bound (rule of three: fewer than {300 / n:.2f}% for {n:,} paths), not as impossibility.

## 6. Limitations
- A stylised bank calibrated on system-wide aggregates; not a model of any individual institution and not a regulatory stress test.
- Stress run-offs are assumptions; conclusions are conditional on them.
- {horizon_limit}. Asset-side stress (fire-sale haircuts, credit-line drawdowns) is not modelled.
- The bootstrap can only reuse the {int(s['n_changes'])} observed {unit}ly changes, so its tails are limited by the sample.{small_sample}

## 7. Conclusion
{breaking} {model_txt.split('. ')[0]}. For a risk manager, the practical output is the buffer table: it translates a chosen confidence level and stress assumption into a liquid-asset target, and the sensitivity grid shows how quickly risk falls as that target rises.

## Reproducibility
`python src/liquidity_stress.py --csv {csv_name}{freq_flag}` reproduces every number and figure (data fingerprint `{ctx['fp']}`).
"""

    readme = f"""{demo_banner}# Monte Carlo Liquidity Stress Testing — UAE Banking Sector

Monte Carlo simulation of 30-day deposit outflows for a stylised UAE bank, calibrated on Central Bank of the UAE aggregate deposit statistics ({period}).

**Headline:** with liquid assets of {liq_pct:.0f}% of deposits, {head}. Surviving 99% of Severe-scenario paths needs about {sev['buffer99_pct_of_deposits']:.1f}% of deposits in liquid assets.

![Sensitivity heatmap](figures/sensitivity_heatmap.png)

## Method in brief
- Historical {unit}ly deposit growth ({int(s['n_changes'])} observations{scale_short}) gives the volatility of ordinary flows (normal fit and bootstrap).
- Five hypothetical stress scenarios add Beta-distributed run-offs (0–25% mean).
- {n:,} paths per scenario; outputs are exhaustion probability, 99th-percentile outflow, expected shortfall and the liquidity buffer needed for 95/99/99.9% survival.
- Validation: normal vs bootstrap comparison and Monte Carlo convergence (1k/10k/50k paths).

Full write-up: [report/research_report.md](report/research_report.md)

## Results
| Scenario | P(liquidity exhausted) | 99% buffer (% of deposits) |
|---|---|---|
""" + "\n".join(f"| {x} | {fmt_p(boot.loc[x, 'prob_exhaust_pct'], n)}% | "
                 f"{boot.loc[x, 'buffer99_pct_of_deposits']:.1f}% |" for x in order) + f"""

## Reproduce
```
pip install -r requirements.txt
python src/liquidity_stress.py --csv {csv_name}{freq_flag}
```
The input data are not redistributed here. `DATA.md` explains how to rebuild `{csv_name}` from the CBUAE Monthly Statistical Bulletin.

## Repository
```
src/liquidity_stress.py   all code: data check, simulation, figures, write-up
results/                  derived statistics (CSV)
figures/                  charts
report/research_report.md full report
DATA.md                   data source and attribution
```

Data source: Central Bank of the UAE. Analysis and code by {ctx['author']}.
"""
    return report, readme


# ======================================================================= main
def main():
    ap = argparse.ArgumentParser(description="UAE bank liquidity Monte Carlo stress test")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--csv", help="real deposits file (.csv or .xlsx)")
    mode.add_argument("--check", metavar="FILE", help="run the data-quality check only")
    mode.add_argument("--demo", action="store_true", help="synthetic data, pipeline test only")
    ap.add_argument("--frequency", choices=list(FREQS), default="monthly",
                    help="spacing of the input data (quarterly is scaled to a 30-day horizon)")
    ap.add_argument("--paths", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--deposits0", type=float, default=10.0, help="AED bn")
    ap.add_argument("--liquidity0", type=float, default=2.5, help="AED bn")
    ap.add_argument("--keep-drift", action="store_true")
    ap.add_argument("--dayfirst", action="store_true")
    ap.add_argument("--allow-large-moves", action="store_true",
                    help="run even if the data check flags moves (only if verified against the bulletin)")
    ap.add_argument("--author", default="Your Name")
    a = ap.parse_args()
    P.update(FREQS[a.frequency])

    if a.check:
        df = load_deposits(a.check, a.dayfirst)
        print(f"[data] {len(df)} {P['unit']}s, {df.Date.min().date()} -> {df.Date.max().date()}")
        issues, warns = quality_report(df)
        print_quality(issues, warns, P["gate"])
        sys.exit(1 if issues else 0)

    demo = a.demo
    if demo:
        df, fp = demo_data(), "synthetic"
        base = os.path.join(ROOT, "demo_output")
    else:
        df = load_deposits(a.csv, a.dayfirst)
        with open(a.csv, "rb") as f:
            fp = hashlib.sha256(f.read()).hexdigest()[:12]
        base = ROOT

    first, last = df.Date.iloc[0].date(), df.Date.iloc[-1].date()
    bar = "=" * 76
    print(f"{bar}\n  {'*** DEMO: SYNTHETIC DATA - NOT REAL RESULTS ***' if demo else 'REAL DATA RUN'}\n"
          f"  source: {'synthetic' if demo else os.path.basename(a.csv)}   fingerprint: {fp}\n"
          f"  period: {first} -> {last}  ({len(df)} {P['unit']}s, {a.frequency})\n{bar}")

    issues, warns = quality_report(df)
    print_quality(issues, warns, P["gate"])
    if issues and not a.allow_large_moves:
        print("\nSTOPPED before simulation: results from this file would not be meaningful. "
              "Nothing was written.")
        sys.exit(1)

    res, figs, rep = (os.path.join(base, x) for x in ("results", "figures", "report"))
    for p in (res, figs, rep):
        os.makedirs(p, exist_ok=True)

    g = df["Deposits"].pct_change().dropna().reset_index(drop=True)
    k = P["months"]
    gv = (g.mean() / k + (g - g.mean()) / np.sqrt(k)).values   # 30-day-equivalent growth draws
    wi = int(g.idxmin()) + 1
    s = {"data_source": "DEMO" if demo else "REAL", "first_date": str(first), "last_date": str(last),
         "frequency": a.frequency, "n_changes": len(g), "mean_growth_pct": 100 * g.mean(),
         "sd_growth_pct": 100 * g.std(ddof=1), "skew": float(g.skew()),
         "excess_kurtosis": float(g.kurt()), "worst_period_growth_pct": 100 * g.min(),
         "worst_period": df.Date.iloc[wi].strftime("%Y-%m"),
         "p05_growth_pct": 100 * float(np.percentile(g, 5)),
         "mean_30day_equiv_pct": 100 * g.mean() / k, "sd_30day_equiv_pct": 100 * g.std(ddof=1) / np.sqrt(k)}
    pd.DataFrame([s]).to_csv(os.path.join(res, "historical_summary.csv"), index=False)
    print(f"\n=== Historical {P['unit']}ly deposit growth ===")
    for key in list(s)[3:]:
        print(f"  {key:<26}{s[key]:.3f}" if isinstance(s[key], float) else f"  {key:<26}{s[key]}")

    rows, sims = [], {}
    for model in MODELS:
        for name, p in SCENARIOS.items():
            rng = np.random.default_rng(a.seed)
            w = simulate_outflow(gv, model, p["vol_mult"], p["runoff_mean"], a.paths, rng, a.keep_drift)
            sims[(model, name)] = w
            rows.append({"model": model, "scenario": name, **p, "paths": a.paths,
                         **metrics(w, a.deposits0, a.liquidity0)})
    sm = pd.DataFrame(rows)
    sm.to_csv(os.path.join(res, "scenario_metrics.csv"), index=False)
    pd.set_option("display.width", 200)
    print(f"\n=== Scenarios (deposits {a.deposits0:g}bn, liquidity {a.liquidity0:g}bn, {a.paths:,} paths) ===")
    print(sm[["model", "scenario", "prob_exhaust_pct", "mc_std_error_pct", "p99_outflow_pct",
              "es99_outflow_pct", "buffer99_bn", "buffer99_pct_of_deposits"]].round(2).to_string(index=False))

    ratios = [0.10, 0.15, 0.20, 0.25, 0.30, 0.35]
    grid = pd.DataFrame({f"{int(round(r * 100))}%": {sc: 100 * float((sims[("Bootstrap", sc)] > r).mean())
                                                    for sc in SCENARIOS} for r in ratios})
    grid.index.name = "scenario"
    grid.to_csv(os.path.join(res, "sensitivity_liquidity_ratio.csv"))
    print("\n=== P(exhausted, %) by liquid assets / deposits (bootstrap) ===")
    print(grid.round(2).to_string())

    conv = []
    for n in (1000, 10000, 50000):
        rng = np.random.default_rng(a.seed)
        p = SCENARIOS["Severe"]
        m = metrics(simulate_outflow(gv, "Bootstrap", p["vol_mult"], p["runoff_mean"], n, rng, a.keep_drift),
                    a.deposits0, a.liquidity0)
        conv.append({"paths": n, "prob_exhaust_pct": m["prob_exhaust_pct"],
                     "mc_std_error_pct": m["mc_std_error_pct"], "buffer99_bn": m["buffer99_bn"]})
    conv = pd.DataFrame(conv)
    conv.to_csv(os.path.join(res, "convergence.csv"), index=False)
    print("\n=== Convergence (Severe, bootstrap) ===")
    print(conv.round(3).to_string(index=False))

    # ---- figures
    stamp = (f"{'SYNTHETIC DEMO DATA' if demo else 'Source: Central Bank of the UAE'} | "
             f"{first} to {last} | {a.paths:,} paths, seed {a.seed}")

    def save(name):
        fig = plt.gcf()
        fig.text(0.995, 0.005, stamp, ha="right", va="bottom", fontsize=7, color="red" if demo else "gray")
        fig.tight_layout(rect=(0, 0.03, 1, 1))
        fig.savefig(os.path.join(figs, name), dpi=150)
        plt.close(fig)

    plt.figure(figsize=(9, 5))
    gr = g.values
    plt.hist(100 * gr, bins=min(30, max(8, len(gr) // 2)), density=True, alpha=0.7, label="empirical")
    xs = np.linspace(100 * gr.min() - 1, 100 * gr.max() + 1, 300)
    plt.plot(xs, norm.pdf(xs, 100 * gr.mean(), 100 * gr.std(ddof=1)), label="normal fit")
    plt.xlabel(f"{P['unit']}ly deposit growth (%)"); plt.ylabel("density"); plt.legend()
    plt.title(f"Historical {P['unit']}ly deposit growth ({len(gr)} observations)")
    save("historical_deposit_growth.png")

    plt.figure(figsize=(10, 5.5))
    for sc in SCENARIOS:
        end = a.liquidity0 - sims[("Bootstrap", sc)] * a.deposits0
        plt.hist(end, bins=120, density=True, histtype="step", lw=1.4, label=sc)
    plt.axvline(0, color="k", ls="--")
    plt.xlabel("closing liquidity after 30 days (AED bn); left of the dashed line = exhausted")
    plt.ylabel("density"); plt.legend(); plt.title("Simulated closing liquidity by scenario (bootstrap)")
    save("closing_liquidity.png")

    b = sm[sm.model == "Bootstrap"].set_index("scenario")[["buffer95_bn", "buffer99_bn"]]
    b.columns = ["95% survival", "99% survival"]
    b.plot(kind="bar", figsize=(9, 5))
    plt.axhline(a.liquidity0, color="r", ls="--", label=f"liquidity held ({a.liquidity0:g}bn)")
    plt.ylabel("AED bn"); plt.xlabel(""); plt.legend(); plt.xticks(rotation=0)
    plt.title("Liquidity buffer needed to survive 95% / 99% of scenarios")
    save("required_buffer.png")

    fig, ax = plt.subplots(figsize=(8, 4.5))
    im = ax.imshow(grid.values, cmap="Reds", aspect="auto", vmin=0, vmax=100)
    ax.set_xticks(range(len(grid.columns)), grid.columns)
    ax.set_yticks(range(len(grid.index)), grid.index)
    for i in range(grid.shape[0]):
        for j in range(grid.shape[1]):
            val = grid.values[i, j]
            ax.text(j, i, f"{val:.1f}", ha="center", va="center", fontsize=8,
                    color="white" if val > 60 else "black")
    ax.set_xlabel("liquid assets / deposits"); ax.set_title("P(liquidity exhausted, %) - bootstrap")
    fig.colorbar(im)
    save("sensitivity_heatmap.png")

    # ---- write-up
    ctx = {"s": s, "a": a, "boot": sm[sm.model == "Bootstrap"].set_index("scenario"),
           "nrm": sm[sm.model == "Normal"].set_index("scenario"), "conv": conv, "grid": grid,
           "demo": demo, "fp": fp, "author": a.author,
           "csv_name": "data/" + (os.path.basename(a.csv) if a.csv else "cbuae_deposits.csv")}
    report, readme = build_texts(ctx)
    with open(os.path.join(rep, "research_report.md"), "w", encoding="utf-8") as f:
        f.write(report)
    with open(os.path.join(base, "README.md"), "w", encoding="utf-8") as f:
        f.write(readme)
    with open(os.path.join(res, "run_manifest.json"), "w") as f:
        json.dump({**{key: s[key] for key in ("data_source", "first_date", "last_date", "frequency", "n_changes")},
                   "file_fingerprint": fp, "paths": a.paths, "seed": a.seed, "deposits0_bn": a.deposits0,
                   "liquidity0_bn": a.liquidity0, "keep_drift": a.keep_drift,
                   "allow_large_moves": a.allow_large_moves, "runoff_cv": RUNOFF_CV,
                   "scenarios": SCENARIOS}, f, indent=2)

    where = base
    print(f"\n{bar}\n  DONE ({'DEMO - do not publish' if demo else 'REAL DATA'}). Written to {where}/:\n"
          f"  results/  figures/  report/research_report.md  README.md\n{bar}")


if __name__ == "__main__":
    main()
