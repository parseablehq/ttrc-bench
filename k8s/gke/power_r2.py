"""Power of the run-2 design (benchmark/RUN2-PREREGISTRATION.md section 7): n instances, k cells per stack per
instance, paired across stacks. Pure Python, seeded, about a minute.

Per instance i the baseline stack succeeds with p_i from a two-point mixture (a ceiling group at 0.95 and a hard
group at 0.35; run 1 had about 80% of instances at ceiling). The comparison stack has the same p_i less `delta` on
a random `hit` share of the instances (a tool-surface defect hurts some questions, not all). Each simulation draws
k Bernoulli cells per instance per stack, takes the paired difference of per-instance means, and a cluster
bootstrap over instances gives the 95% percentile interval. Power is the share of simulations whose interval
excludes 0. Also printed: the half-width of one stack's accuracy interval.

    python k8s/gke/power_r2.py

On 2026-10-30 it printed, for 30 instances at k=5: power 0.82 for a 0.5 drop on 30% of instances (15 points), 0.54
for 0.5 on 20% (10 points), 0.29 for 0.8 on 10% (8 points); one-stack half-width 0.094. At k=3: 0.75, 0.41, 0.24.
"""

import random
import statistics

rng = random.Random(20261031)
N_SIM, B = 150, 600


def pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def sim(n: int, k: int, delta: float, hit: float, ceiling_share: float = 0.8) -> tuple[float, float]:
    rejects, halfwidths = 0, []
    for _ in range(N_SIM):
        p = [0.95 if rng.random() < ceiling_share else 0.35 for _ in range(n)]
        q = [min(0.98, max(0.02, pi - delta)) if rng.random() < hit else pi for pi in p]
        da = [sum(rng.random() < pi for _ in range(k)) / k for pi in p]
        db = [sum(rng.random() < qi for _ in range(k)) / k for qi in q]
        d = [x - y for x, y in zip(da, db, strict=True)]
        boot_d, boot_a = [], []
        for _ in range(B):
            idx = [rng.randrange(n) for _ in range(n)]
            boot_d.append(sum(d[i] for i in idx) / n)
            boot_a.append(sum(da[i] for i in idx) / n)
        lo, hi = pct(boot_d, 0.025), pct(boot_d, 0.975)
        rejects += lo > 0 or hi < 0
        halfwidths.append((pct(boot_a, 0.975) - pct(boot_a, 0.025)) / 2)
    return rejects / N_SIM, statistics.mean(halfwidths)


if __name__ == "__main__":
    print("n k delta hit -> power, one-stack accuracy CI half-width (mean drop in accuracy points)")
    for n, k in ((30, 5), (30, 3), (25, 3)):
        for delta, hit in ((0.5, 0.2), (0.5, 0.3), (0.8, 0.1), (0.8, 0.2)):
            power, hw = sim(n, k, delta, hit)
            print(f"{n:3d} {k} {delta:.1f} {hit:.1f} -> {power:.2f}  {hw:.3f}   ({100 * delta * hit:.0f} points)")
