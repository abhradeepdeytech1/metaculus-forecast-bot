"""Strict parsing, median aggregation, and Metaculus distribution constraints."""
from bisect import bisect_right
from dataclasses import dataclass
import json
import math
from statistics import median

PERCENTILES = (.05, .1, .25, .5, .75, .9, .95)


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Expected a finite number")
    return float(value)


@dataclass(frozen=True)
class Question:
    post_id: int
    id: int
    kind: str
    title: str
    description: str = ""
    criteria: str = ""
    fine_print: str = ""
    options: tuple = ()
    lo: float = 0
    hi: float = 1
    zero: float | None = None
    open_lo: bool = False
    open_hi: bool = False
    size: int = 201
    close_time: str | None = None
    status: str = "open"
    unit: str = ""
    nominal_lo: float | None = None
    nominal_hi: float | None = None

    @classmethod
    def from_post(cls, post):
        q = post.get("question")
        if not isinstance(q, dict) or post.get("group_of_questions") or post.get("conditional"):
            raise ValueError("Only standalone tournament questions are supported")
        kind = q["type"]
        if kind not in {"binary", "numeric", "discrete", "multiple_choice"}:
            raise ValueError("Unsupported question type")
        scaling = q.get("scaling") or {}
        size = 201
        if kind in {"numeric", "discrete"}:
            count = scaling.get("inbound_outcome_count", q.get("inbound_outcome_count"))
            if kind == "numeric" and count is None:
                count = 200
            if type(count) is not int or not 1 <= count <= 10000:
                raise ValueError("Missing or invalid discrete outcome count")
            size = count + 1
        options = tuple(q.get("options") or ())
        if kind == "multiple_choice" and (len(options) < 2 or len(set(options)) != len(options) or any(not isinstance(o, str) for o in options)):
            raise ValueError("Invalid multiple choice options")
        lo, hi = number(scaling.get("range_min", 0)), number(scaling.get("range_max", 1))
        zero = scaling.get("zero_point")
        if kind in {"numeric", "discrete"}:
            if any(type(q.get(field)) is not bool for field in ("open_lower_bound", "open_upper_bound")):
                raise ValueError("Numeric questions require explicit open or closed bounds")
            if "range_min" not in scaling or "range_max" not in scaling or lo >= hi:
                raise ValueError("Invalid numeric bounds")
            if zero is not None:
                zero = number(zero)
                if lo == zero or (hi - zero) / (lo - zero) <= 0:
                    raise ValueError("Invalid logarithmic zero point")
        return cls(post_id=int(post["id"]), id=int(q["id"]), kind=kind, title=q.get("title") or post["title"],
                   description=q.get("description") or "", criteria=q.get("resolution_criteria") or "",
                   fine_print=q.get("fine_print") or "", options=options, lo=lo, hi=hi, zero=zero,
                   open_lo=bool(q.get("open_lower_bound", False)), open_hi=bool(q.get("open_upper_bound", False)),
                   size=size, close_time=q.get("actual_close_time") or q.get("scheduled_close_time"),
                   status=q.get("status", ""), unit=q.get("unit") or "",
                   nominal_lo=scaling.get("nominal_min"), nominal_hi=scaling.get("nominal_max"))

    def context(self):
        # Whitelist fields so resolution, forecasts, comments, and API account data never enter prompts.
        return json.dumps({"question": self.title, "background": self.description, "resolution_criteria": self.criteria,
                           "fine_print": self.fine_print, "options": self.options, "lower_bound": self.lo,
                           "upper_bound": self.hi, "lower_bound_is_open": self.open_lo,
                           "upper_bound_is_open": self.open_hi, "logarithmic_zero_point": self.zero,
                           "kind": self.kind, "units": self.unit, "nominal_lower_bound": self.nominal_lo,
                           "nominal_upper_bound": self.nominal_hi, "closes": self.close_time}, ensure_ascii=False)


def grid(q):
    if q.zero is None:
        return [q.lo + (q.hi - q.lo) * i / (q.size - 1) for i in range(q.size)]
    ratio = (q.hi - q.zero) / (q.lo - q.zero)
    return [q.lo + (q.hi - q.lo) * (ratio ** (i / (q.size - 1)) - 1) / (ratio - 1) for i in range(q.size)]


def constrain_cdf(q, cdf):
    if len(cdf) != q.size:
        raise ValueError("Wrong CDF size")
    values = [number(x) for x in cdf]
    if any(not 0 <= x <= 1 for x in values) or any(a > b for a, b in zip(values, values[1:])):
        raise ValueError("Invalid CDF")
    first = min(.999, max(.001, values[0])) if q.open_lo else 0.0
    last = max(.001, min(.999, values[-1])) if q.open_hi else 1.0
    # Apply endpoints before differences so a spike at a closed bound keeps its mass.
    values[0], values[-1] = first, last
    n = q.size - 1
    minimum = .01 / n
    maximum = min(1.0, .2 * 200 / n)
    # Prevent almost all mass being placed outside an open range when both tails are open.
    if last - first < .01:
        center = min(.994, max(.006, (first + last) / 2))
        first, last = max(.001, center - .005), min(.999, center + .005)
    target = last - first
    if target < n * minimum - 1e-12 or target > n * maximum + 1e-12:
        raise ValueError("Infeasible CDF bounds")
    weights = [max(0.0, b - a) for a, b in zip(values, values[1:])]
    # Euclidean projection of bin masses onto a bounded simplex.
    left, right = -2.0, 2.0
    for _ in range(90):
        shift = (left + right) / 2
        total = sum(min(maximum, max(minimum, w + shift)) for w in weights)
        if total < target:
            left = shift
        else:
            right = shift
    masses = [min(maximum, max(minimum, w + (left + right) / 2)) for w in weights]
    result = [first]
    for mass in masses:
        result.append(result[-1] + mass)
    result[-1] = last
    return result


def quantiles_to_cdf(q, quantiles):
    if not isinstance(quantiles, list) or len(quantiles) != len(PERCENTILES):
        raise ValueError("Seven forecast quantiles are required")
    pairs = [(number(item["p"]), number(item["value"])) for item in quantiles]
    if tuple(p for p, _ in pairs) != PERCENTILES:
        raise ValueError("Unexpected quantile probabilities or order")
    xs = [v for _, v in pairs]
    if any(a > b for a, b in zip(xs, xs[1:])):
        raise ValueError("Quantiles must increase")
    if (not q.open_lo and xs[0] < q.lo) or (not q.open_hi and xs[-1] > q.hi):
        raise ValueError("Quantiles exceed closed bounds")
    span = max(xs[-1] - xs[0], (q.hi - q.lo) / 20)
    # Extrapolate tails rather than silently discarding mass beyond open bounds.
    anchors = [(xs[0] - span, 0.0), *[(v, p) for p, v in pairs], (xs[-1] + span, 1.0)]
    if not q.open_lo:
        anchors.insert(0, (q.lo, 0.0))
        anchors = sorted((x, p) for x, p in anchors if x >= q.lo)
    if not q.open_hi:
        anchors.append((q.hi, 1.0))
        anchors = sorted((x, p) for x, p in anchors if x <= q.hi)
    points = [x for x, _ in anchors]
    cdf = []
    for x in grid(q):
        i = bisect_right(points, x)
        if i == 0:
            p = anchors[0][1]
        elif i == len(anchors):
            p = anchors[-1][1]
        else:
            x0, p0 = anchors[i - 1]
            x1, p1 = anchors[i]
            p = p0 + (p1 - p0) * (x - x0) / (x1 - x0)
        cdf.append(p)
    return constrain_cdf(q, cdf)


def parse_prediction(q, response):
    if not isinstance(response, dict) or not isinstance(response.get("reasoning"), str) or not response["reasoning"].strip():
        raise ValueError("Forecast must include reasoning")
    if q.kind == "binary":
        p = number(response["probability"])
        if not 0 <= p <= 1:
            raise ValueError("Probability outside [0,1]")
        return p
    if q.kind == "multiple_choice":
        probs = response["probabilities"]
        if not isinstance(probs, dict) or set(probs) != set(q.options):
            raise ValueError("Option labels must match the question exactly")
        probs = {o: number(probs[o]) for o in q.options}
        total = sum(probs.values())
        if any(not 0 <= p <= 1 for p in probs.values()) or abs(total - 1) > .02:
            raise ValueError("Invalid option probabilities")
        return {o: p / total for o, p in probs.items()}
    return quantiles_to_cdf(q, response["quantiles"])


def aggregate(q, predictions, floor=.02):
    if not predictions:
        raise ValueError("No valid ensemble predictions")
    if q.kind == "binary":
        return min(1 - floor, max(floor, median(predictions)))
    if q.kind == "multiple_choice":
        probs = {o: median(p[o] for p in predictions) for o in q.options}
        total = sum(probs.values())
        normalized = {o: p / total if total else 1 / len(probs) for o, p in probs.items()}
        minimum = min(floor, .5 / len(probs))
        return {o: minimum + (1 - len(probs) * minimum) * p for o, p in normalized.items()}
    return constrain_cdf(q, [median(p[i] for p in predictions) for i in range(q.size)])


def payload(q, prediction):
    field = "probability_yes" if q.kind == "binary" else "probability_yes_per_category" if q.kind == "multiple_choice" else "continuous_cdf"
    return {"question": q.id, field: prediction}
