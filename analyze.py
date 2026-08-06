#!/usr/bin/env python
# coding: utf-8
"""
Analyze attempts.jsonl from the self-learning experiment.

Reports per-tier HIT RATES (not averages), since the offer is a discrete ladder
and what matters is how often each tier (especially the top) gets pulled. For
each card and each randomized factor it shows the top-tier hit-rate per level and
runs a permutation test on the top-tier indicator: does this factor change the
probability of pulling the top offer, or is it flat (random)?
"""
import json
import sys
from datetime import datetime

import numpy as np
import pandas as pd

LOG = sys.argv[1] if len(sys.argv) > 1 else "attempts.jsonl"
FACTORS = ["fp_viewport", "device", "is_mobile", "method", "city",
           "fp_platform", "fp_timezone", "dwell_s", "exit_subnet", "hour_bucket"]


def f_stat(values, codes):
    grand = values.mean()
    ssb = ssw = 0.0
    k = 0
    for g in np.unique(codes):
        v = values[codes == g]
        if len(v) == 0:
            continue
        k += 1
        ssb += len(v) * (v.mean() - grand) ** 2
        ssw += ((v - v.mean()) ** 2).sum()
    n = len(values)
    if k < 2 or n - k <= 0:
        return None
    if ssw == 0:
        return np.inf if ssb > 0 else 0.0
    return (ssb / (k - 1)) / (ssw / (n - k))


def perm_test(values, labels, n_perm=3000):
    """Permutation test. values here is the top-tier indicator (1/0)."""
    values = np.asarray(values, float)
    codes = pd.Categorical(labels).codes
    obs = f_stat(values, codes)
    if obs is None:
        return None, None
    if not np.isfinite(obs):
        return obs, 0.0
    ge = 1
    for _ in range(n_perm):
        if f_stat(np.random.permutation(values), codes) >= obs:
            ge += 1
    return obs, ge / (n_perm + 1)


def subnet16(ip):
    if not isinstance(ip, str):
        return None
    p = ip.split(".")
    return ".".join(p[:2]) + ".x.x" if len(p) == 4 else None


def hour_bucket(ts):
    try:
        h = datetime.fromisoformat(ts).hour
    except Exception:
        return None
    return ["night", "morning", "afternoon", "evening"][h // 6]


def tier_pct(series):
    """Return 'tierA NN%, tierB NN%' descending by tier value."""
    n = len(series)
    vc = series.value_counts()
    parts = []
    for tier in sorted(vc.index, reverse=True):
        parts.append(f"{int(tier/1000)}k {100*vc[tier]/n:.0f}%")
    return ", ".join(parts)


def analyze_factor(sub, factor, target):
    g = sub.dropna(subset=[factor, "exposed_offer_points"]).copy()
    g = g[g.groupby(factor)[factor].transform("size") >= 3]
    if g[factor].nunique() < 2 or len(g) < 8:
        return None
    g["top"] = (g["exposed_offer_points"] >= target).astype(int)
    obs, p = perm_test(g["top"].values, g[factor].values)
    if obs is None:
        return None
    tab = (g.groupby(factor)
           .agg(n=("top", "size"), top_hits=("top", "sum")))
    tab["top_rate"] = tab["top_hits"] / tab["n"]
    tab = tab.sort_values("top_rate", ascending=False)
    spread = tab["top_rate"].max() - tab["top_rate"].min()
    return {"factor": factor, "p": p, "levels": g[factor].nunique(),
            "n": len(g), "spread": spread, "table": tab}


def main():
    rows = [json.loads(l) for l in open(LOG)]
    att = pd.DataFrame([r for r in rows if r.get("type") == "attempt"])
    if att.empty:
        print("No attempts yet.")
        return
    att["exit_subnet"] = att.get("exit_ip", pd.Series(dtype=object)).map(subnet16)
    att["hour_bucket"] = att.get("timestamp", pd.Series(dtype=object)).map(hour_bucket)

    n = len(att)
    ok = att[att["exposed_offer_points"].notna()].copy()
    print(f"= EXPERIMENT ANALYSIS ({LOG}) =")
    print(f"attempts: {n} | successful exposures: {len(ok)} ({100*len(ok)/n:.0f}%)")
    if "error" in att:
        errs = att[att["error"].notna()]["error"].map(lambda e: e.split(":")[0]).value_counts()
        if len(errs):
            print("errors:", dict(errs))
    if ok.empty:
        print("No successful exposures yet.")
        return

    for card in sorted(ok["card"].dropna().unique()):
        sub = ok[ok["card"] == card]
        target = int(sub["target"].iloc[0])
        top_rate = 100 * (sub["exposed_offer_points"] >= target).mean()
        print(f"\n## {card}  (n={len(sub)}, top tier {int(target/1000)}k)")
        print(f"   TOP-TIER ({int(target/1000)}k) pulled {top_rate:.0f}% of the time")
        print(f"   tier mix: {tier_pct(sub['exposed_offer_points'])}")
        results = [r for r in (analyze_factor(sub, f, target) for f in FACTORS) if r]
        results.sort(key=lambda r: r["p"])
        if not results:
            print("   not enough data per factor yet.")
            continue
        print(f"   {'factor':13} {'levels':>6} {'n':>4} {'p':>7} {'top-rate spread':>16}  verdict")
        for r in results:
            verdict = ("EFFECT - exploit" if r["p"] < 0.05 and r["spread"] >= 0.15
                       else "weak/maybe" if r["p"] < 0.20 else "flat (random)")
            print(f"   {r['factor']:13} {r['levels']:>6} {r['n']:>4} "
                  f"{r['p']:>7.3f} {100*r['spread']:>14.0f}%  {verdict}")
        top = results[0]
        if top["p"] < 0.20:
            print(f"   -- {top['factor']} top-tier hit-rate by level --")
            for lvl, row in top["table"].head(8).iterrows():
                print(f"      {str(lvl):16} {int(row['top_hits'])}/{int(row['n'])} "
                      f"= {100*row['top_rate']:.0f}% pulled {int(target/1000)}k")

    print("\nGuide: top-tier hit-rate = how often the best bonus was pulled. "
          "p<0.05 with a big rate spread = that factor really changes your odds "
          "of the top offer. p>=0.20 = flat (does not matter).")


if __name__ == "__main__":
    main()
