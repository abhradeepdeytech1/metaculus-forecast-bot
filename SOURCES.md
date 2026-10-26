# Sources checked on 2026-10-27

## Upstream reference

`template-upstream/` is a clean clone of [Metaculus/metac-bot-template](https://github.com/Metaculus/metac-bot-template) at commit `8c898aa5bacf8f49b9ec71964be5f932741f69d2`. The working bot adapts the direct API approach in `main_with_no_framework.py` and retains the tournament workflow's 20-minute schedule. The active runtime is in `bot/`; the reference clone is not deployed.

The working bot adds a durable question and cost ledger, atomic forecast/comment submission, and strict JSON parsing without paid repair calls. It now defaults to six prediction calls across Sonnet and Opus through the direct Anthropic API. The [Fall 2025 survey](https://www.metaculus.com/notebooks/43337/fall-2025-futureeval-survey/) reported about 28 calls and $0.90 per question for the median winner. Six calls are a budget choice, not evidence of equal accuracy. Resolved-question runs check the pipeline only; current news and model memory prevent treating them as an unbiased benchmark.

## Tournament rules and schedule

The [official resource page](https://www.metaculus.com/notebooks/38928/bot-tournament-resources-page/) lists binary, numeric, discrete and multiple-choice questions. Questions open at random times for three hours, sometimes in concurrent batches. Submit once per question. Each person or team has one prize-eligible primary bot. Forecasts and reasoning comments must be posted directly through the API, with private comments requested. Humans must not select reruns or tune the bot after previewing open or upcoming tournament forecasts. Development can use closed questions or questions outside the tournament. Fully autonomous bots are allowed; humans still create the human account and handle identification or interviews. The participation form is required.

Use project `fall-futureeval-2026` / `33121`, `minibench` for the active MiniBench, and `bot-testing-area` / `32977` for unscored testing. MiniBench starts every other Monday and pays $1,000 per round. The next start inferred from the published September 21 cadence is November 2, 2026; verify the active round before operating. The [Fall tournament page](https://www.metaculus.com/tournament/fall-futureeval-2026/) lists September 28, 2026 through January 6, 2027 and a $50,000 prize pool.

## Forecasting method

The official Python framework provides a useful reference, not a requirement. Its [binary aggregation](https://github.com/Metaculus/forecasting-tools/blob/main/forecasting_tools/data_models/binary_report.py) takes the median. Its [numeric aggregation](https://github.com/Metaculus/forecasting-tools/blob/main/forecasting_tools/data_models/numeric_report.py) converts forecasts onto the same CDF grid and takes the median at each grid location. Discrete questions use the numeric machinery. Its [multiple-choice aggregation](https://github.com/Metaculus/forecasting-tools/blob/main/forecasting_tools/data_models/multiple_choice_report.py) checks exact option-name agreement and takes the arithmetic mean for each option. Taking optionwise medians and normalizing is a local design choice, not the framework's method.

In the [official Q4 2024 analysis](https://www.metaculus.com/notebooks/35291/q4-ai-benchmarking-results/), winner pgodzinai grouped related questions for coherent forecasts, combined AskNews and Perplexity research, and generated eight forecasts with two models. It dropped the two most extreme forecasts and averaged the other six. Most surveyed winning bots used repeated forecasts and mean or median aggregation. These historical model choices are evidence about process, not current model recommendations.

The [Q2 2025 winner's repository](https://github.com/Panshul42/Forecasting_Bot_Q2) separates historical reference-class reasoning from causal reasoning and uses five prediction agents across model families. This supports separate research and forecast steps, plus an ensemble. Its code is AGPL-3.0; this bot does not copy it. [Mantic's own research writeup](https://thinkingmachines.ai/news/training-llms-to-predict-world-events/) also separates research and prediction, with specialized probability-distribution outputs.

The [Spring 2026 bot-maker advice](https://www.metaculus.com/notebooks/45336/bot-maker-advice-spring-2026/) favors reliable submissions, current research, careful resolution criteria, budget limits and useful logs. Its authors warn that survey responses are partly hypotheses rather than controlled experiments. Extremization, elaborate roles and Monte Carlo approaches received mixed or negative reports. No automatic extremization or Monte Carlo layer is justified by these sources alone.

## API and CDF requirements

The [official API documentation](https://www.metaculus.com/api/) requires authentication, returns unavailable fields as null, and distinguishes post IDs from question IDs. Own forecasts remain accessible. Community aggregates may be unavailable, so they must not be required research inputs.

The [official OpenAPI specification](https://github.com/Metaculus/metaculus/blob/main/docs/openapi.yml) defines these forecast payloads:

- Binary: `{"question": QUESTION_ID, "probability_yes": 0.63}`.
- Multiple choice: `{"question": QUESTION_ID, "probability_yes_per_category": {"Exact option": 0.4, "Other exact option": 0.6}}`.
- Numeric or discrete: `{"question": QUESTION_ID, "continuous_cdf": [...]}`.

Numeric CDFs normally have 201 values. Discrete CDFs have `inbound_outcome_count + 1`, as confirmed by the [official question models](https://github.com/Metaculus/forecasting-tools/blob/main/forecasting_tools/data_models/questions.py). Respect the question's actual scaling grid and logarithmic zero point. Discrete range boundaries may sit half a bucket beyond nominal outcomes.

For N inbound outcomes, each adjacent CDF increment must be at least `0.01 / N` and at most `0.2 * 200 / N`. Closed lower and upper bounds require first value 0 and last value 1. An open lower bound needs first value at least 0.001; an open upper bound needs last value at most 0.999. A closed lower-bound CDF value represents mass strictly below the bound; mass at the bound belongs in the first inbound bucket. Validate finite values, correct length, monotonicity, increment limits and tail constraints before publishing. The numeric framework contains a comment typo, `0.01 / 200 = 0.0005`; the correct value is 0.00005.

## Atomic forecast and comment publication

The [bulk endpoint specification](https://github.com/Metaculus/metaculus/blob/main/docs/openapi.yml#L2491) documents `POST /api/questions/bulk-forecast-comment/`. It accepts an object containing `forecasts` and `comments` arrays. Comments require `on_post`, `text`, and `is_private: true`; set `included_forecast: true` to attach the latest forecast. Optional `acting_user` selects an owned bot; with a bot token, omit it and use the authenticated account. A valid request saves the forecasts and comments in one transaction. Any validation error saves nothing. Documented statuses are 201, 400 and 403. The [current handler](https://github.com/Metaculus/metaculus/blob/main/questions/views.py) confirms the transaction and returns an empty object on success. The server assigns the API source; `source` is omitted from the request because it is absent from the accepted forecast serializer fields.

```json
{
  "forecasts": [{"question": 123, "probability_yes": 0.63}],
  "comments": [{
    "on_post": 456,
    "text": "Research, reasoning and final forecast.",
    "is_private": true,
    "included_forecast": true
  }]
}
```

No authenticated API calls, paid model calls, forecast submissions or scoring runs were used to check these sources. Live gateway support for new endpoints still needs validation when live operation is authorized.

## Direct Anthropic models and pricing

Anthropic's [Sonnet 5.5 model page](https://platform.claude.com/docs/en/models/sonnet-5-5/overview) confirms the direct API ID `claude-sonnet-5-5`, $2 per million uncached input tokens, $10 output, $2.50 for 5-minute cache writes, and $0.10 cache reads. Its [Opus 5.5 release documentation](https://platform.claude.com/docs/en/models/opus-5-5/whats-new-opus-5-5) confirms `claude-opus-5-5` at $4 input, $20 output, $5 for 5-minute cache writes, and $0.20 cache reads. The [pricing page](https://platform.claude.com/docs/en/about-claude/pricing) confirms these standard rates. Both model IDs and all four billing categories were confirmed on October 27, 2026; the code also allows explicit configured rate updates.

The default mix is four Sonnet calls and two Opus calls with low effort and a 4,096-token output cap. The README shows an illustrative $0.41568 cost with cache hits, $0.49568 with no caching, and $0.53568 if every call writes a cold cache. These scenarios use a 10,000-token prefix and 500-token suffix per call. They do not establish forecast quality or measured average spend. OpenRouter remains available only for explicitly selected models and an OpenRouter key; no default call or fallback uses it. The $200 monthly Anthropic credit allowance is the user's stated feasibility budget, not a balance checked by this bot.

The [prompt caching guide](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) requires an identical prefix before the cache breakpoint. The bot caches the shared question and research, with sample instructions and timestamps afterward. Five-minute writes cost 1.25 times base input prices. The new models' reads cost 5% of base input. Usage reports uncached input, cache creation and cache reads separately, so they must not be counted twice. Short prompts can miss the 512-token threshold. Samples are sequential and grouped per model; caches are not shared between models.

The [official SDK v0.79.0 request source](https://github.com/anthropics/anthropic-sdk-python/blob/v0.79.0/src/anthropic/resources/messages/messages.py), [output config](https://github.com/anthropics/anthropic-sdk-python/blob/v0.79.0/src/anthropic/types/output_config_param.py), and [cache control type](https://github.com/anthropics/anthropic-sdk-python/blob/v0.79.0/src/anthropic/types/cache_control_ephemeral_param.py) support the chosen low effort, JSON schema and explicit 5-minute caching. The SDK is configured with no automatic retries. All pricing and request behavior tests use offline fixtures. The durable ledger counts pending ceilings toward question, daily and monthly caps and uses the remaining calendar-month budget to pace daily admission. Existing version-1 ledger records remain valid and retain their costs.

## AskNews SDK request behavior

The exact [AskNews 0.13.11 release](https://pypi.org/project/asknews/0.13.11/) wheel was read as an archive in memory, without installing or executing it. Its `AskNewsSDK` constructor accepts `api_key`, `retries=3`, `timeout=None` and `follow_redirects=True`. In `client.py`, the retries value is stored but not used by the request function, which performs one HTTP send. API-key authentication adds a Bearer header without an authentication retry. OAuth authentication can repeat the original request once after a 401 and token refresh. Set `retries=0`, a finite timeout and `follow_redirects=False`, and use API-key authentication to keep request counts explicit. Do not add an automatic paid-call retry around it. The [upstream source repository](https://github.com/emergentmethods/asknews-python-sdk) provides the SDK code; the checked version comes from the versioned published wheel, since current main can change.
