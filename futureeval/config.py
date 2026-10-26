from dataclasses import dataclass, field
from decimal import Decimal, ROUND_CEILING
import json
import os


def micros(value):
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ValueError("Cost must be finite and nonnegative")
    return int((amount * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


@dataclass(frozen=True)
class Config:
    models: tuple[str, ...] = ("claude-sonnet-5-5", "claude-opus-5-5")
    sample_counts: tuple[int, ...] = (4, 2)
    tournaments: tuple[str, ...] = ("fall-futureeval-2026", "minibench")
    samples: int = 2
    max_tokens: int = 4096
    question_cap: int = 1_000_000
    daily_cap: int = 10_000_000
    monthly_cap: int = 200_000_000
    news_cost: int = 0
    floor: float = 0.02
    anthropic_prices: dict = field(default_factory=lambda: {
        "claude-sonnet-5-5": {"input": 2, "output": 10, "cache_write": 2.5, "cache_read": .1},
        "claude-opus-5-5": {"input": 4, "output": 20, "cache_write": 5, "cache_read": .2},
    })

    @classmethod
    def from_env(cls):
        default = cls()
        models = tuple(x.strip() for x in os.getenv("FORECAST_MODELS", ",".join(default.models)).split(",") if x.strip())
        samples = int(os.getenv("SAMPLES_PER_MODEL") or "2")
        raw_counts = os.getenv("MODEL_SAMPLE_COUNTS", "").strip()
        if raw_counts:
            counts = tuple(int(x.strip()) for x in raw_counts.split(","))
        elif models == default.models and not os.getenv("SAMPLES_PER_MODEL"):
            counts = default.sample_counts
        else:
            counts = (samples,) * len(models)
        prices = default.anthropic_prices.copy()
        raw_prices = os.getenv("ANTHROPIC_PRICES_JSON", "").strip()
        if raw_prices:
            overrides = json.loads(raw_prices)
            if not isinstance(overrides, dict):
                raise ValueError("Anthropic prices must be a model-to-price object")
            prices.update(overrides)
        c = cls(
            models=models, sample_counts=counts,
            tournaments=tuple(x.strip() for x in os.getenv("TOURNAMENT_IDS", ",".join(default.tournaments)).split(",") if x.strip()),
            samples=samples,
            max_tokens=int(os.getenv("MAX_OUTPUT_TOKENS", "4096")),
            question_cap=micros(os.getenv("QUESTION_BUDGET_USD", "1")),
            daily_cap=micros(os.getenv("DAILY_BUDGET_USD", "10")),
            monthly_cap=micros(os.getenv("MONTHLY_BUDGET_USD", "200")),
            news_cost=micros(os.getenv("ASKNEWS_REQUEST_COST_USD", "0")),
            floor=float(os.getenv("PROBABILITY_FLOOR", "0.02")),
            anthropic_prices=prices,
        )
        if len(set(c.models)) < 2 or len(c.models) != len(set(c.models)):
            raise ValueError("Configure at least two distinct forecast models")
        if (len(c.sample_counts) != len(c.models) or sum(c.sample_counts) < 3 or any(not 1 <= n <= 10 for n in c.sample_counts)
                or not 1 <= c.samples <= 10 or not 512 <= c.max_tokens <= 16384):
            raise ValueError("Invalid ensemble size or output token limit")
        if not c.tournaments or c.question_cap <= 0 or c.daily_cap <= 0 or c.monthly_cap <= 0 or not 0 < c.floor < 0.5:
            raise ValueError("Invalid tournament, budget, or probability floor")
        for model in c.models:
            if model.startswith("claude-"):
                price = c.anthropic_prices.get(model)
                if not isinstance(price, dict) or set(price) != {"input", "output", "cache_write", "cache_read"}:
                    raise ValueError("Direct Claude models require all four configured token prices")
                for amount in price.values():
                    if isinstance(amount, bool) or micros(amount) <= 0:
                        raise ValueError("Anthropic token prices must be positive and finite")
            else:
                raw = model.removeprefix("openrouter:")
                if "/" not in raw or ":" in raw or raw.startswith("openrouter/"):
                    raise ValueError("Use direct claude-* IDs or openrouter:author/model IDs without plugins")
        return c
