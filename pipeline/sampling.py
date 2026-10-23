"""Statistics used to check that a random sample represents its population."""

import math

MIN_EXPECTED = 5


def _gamma_series(a, x):
    term = total = 1.0 / a
    for n in range(1, 1000):
        term *= x / (a + n)
        total += term
        if abs(term) < abs(total) * 1e-14:
            break
    return total * math.exp(-x + a * math.log(x) - math.lgamma(a))


def _gamma_continued_fraction(a, x):
    tiny = 1e-300
    b = x + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        d = tiny if abs(d) < tiny else d
        c = b + an / c
        c = tiny if abs(c) < tiny else c
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-14:
            break
    return math.exp(-x + a * math.log(x) - math.lgamma(a)) * h


def chi_square_p_value(statistic, degrees_of_freedom):
    """Upper-tail probability of a chi-square statistic."""
    if degrees_of_freedom <= 0:
        return 1.0
    if statistic <= 0:
        return 1.0
    a, x = degrees_of_freedom / 2.0, statistic / 2.0
    if x < a + 1.0:
        return max(0.0, 1.0 - _gamma_series(a, x))
    return min(1.0, _gamma_continued_fraction(a, x))


def representativeness(population_counts, sample_counts):
    """Goodness of fit of the sample's category counts against the population's proportions.

    Categories whose expected sample count is below MIN_EXPECTED are pooled so the
    chi-square approximation holds. Total variation distance is reported as an
    effect size, because with large samples the p-value alone is too sensitive.
    """
    population_total = sum(population_counts.values())
    sample_total = sum(sample_counts.values())
    if not population_total or not sample_total:
        return {"p_value": None, "chi_square": None, "df": 0, "total_variation": None, "categories": 0}

    observed, expected, pooled_obs, pooled_exp = [], [], 0, 0.0
    for category, count in population_counts.items():
        exp = sample_total * count / population_total
        obs = sample_counts.get(category, 0)
        if exp < MIN_EXPECTED:
            pooled_obs += obs
            pooled_exp += exp
        else:
            observed.append(obs)
            expected.append(exp)
    if pooled_exp >= MIN_EXPECTED:
        observed.append(pooled_obs)
        expected.append(pooled_exp)

    statistic = sum((o - e) ** 2 / e for o, e in zip(observed, expected))
    df = len(observed) - 1
    categories = set(population_counts) | set(sample_counts)
    tv = 0.5 * sum(abs(sample_counts.get(c, 0) / sample_total - population_counts.get(c, 0) / population_total)
                   for c in categories)
    return {"p_value": chi_square_p_value(statistic, df) if df > 0 else None, "chi_square": statistic, "df": df,
            "total_variation": tv, "categories": len(categories)}


def weighted_total_variation(population_counts, weighted_counts):
    """Total variation distance between the population's proportions and the weighted sample's."""
    population_total = sum(population_counts.values())
    weighted_total = sum(weighted_counts.values())
    categories = set(population_counts) | set(weighted_counts)
    return 0.5 * sum(abs(weighted_counts.get(c, 0) / weighted_total - population_counts.get(c, 0) / population_total)
                     for c in categories)
