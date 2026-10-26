# Metaculus FutureEval bot

This bot forecasts new questions in FutureEval and MiniBench. It runs in GitHub Actions, where a durable ledger tracks spend and prevents repeated forecasts. Open questions can launch at any time.

## Accounts and API access

1. Create a human Metaculus account, then create a dedicated bot at [Settings → My Forecasting Bots](https://www.metaculus.com/accounts/settings/bots/). Copy its API token. One prize-eligible bot is allowed per person or team. The human account is the payout identity.
2. Complete the required first section of the [participant form](https://forms.gle/aQdYMq9Pisrf1v7d8). Metaculus closed Fall 2026 LLM credit requests on October 13, 2026. The participation section remains required; Fall credit applications are no longer being reviewed. See the [official tournament resources page](https://www.metaculus.com/notebooks/38928/ai-benchmark-resources/).
3. Create an Anthropic API key for the `$200` monthly API credit allowance supplied with your Claude Max plan. The default models use Anthropic's API directly. The bot does not automate a Claude consumer session. It also has a separate `$200` monthly ledger cap.
4. OpenRouter is optional. Configure `OPENROUTER_API_KEY` only if you select OpenRouter models. Prefix model IDs with `openrouter:` such as `openrouter:openai/gpt-6.1-sol`.
5. AskNews is optional. Bot makers can request AskNews access through the signup steps on the [official resources page](https://www.metaculus.com/notebooks/38928/ai-benchmark-resources/). The published grant is 1,000 calls per month and 4,000 for the tournament. A recent-news request uses one call and an archive request uses five. This bot makes one of each per question, so check the quota before enabling it. Configure either `ASKNEWS_API_KEY` or both `ASKNEWS_CLIENT_ID` and `ASKNEWS_SECRET`. Without AskNews credentials, the bot uses question context alone and has less fresh evidence. It does not silently use another search provider.

Forecasts belong to the bot account. Private forecast comments become public for FutureEval. Keep secrets and personal information out of comments. Metaculus pays prizes to the human participant through its payout process, so provide the participant's own payout identity.

## GitHub Actions setup

1. Put the contents of `bot/` in a new GitHub repository, with `main.py` and `.github/` at its root. Keep `template-upstream/` outside that repository. GitHub does not allow a fork to be private. To keep the `bot-state` ledger private, create a new private repository and copy this project's contents into it. A public repository also works if you accept that its ledger branch exposes question IDs and usage metadata. The ledger has no forecasts or rationales.
2. Enable GitHub Actions. Under **Settings → Secrets and variables → Actions**, add these required repository secrets:

   - `METACULUS_TOKEN`
   - `ANTHROPIC_API_KEY`

   Optional secrets:

   - `OPENROUTER_API_KEY` for model IDs routed through OpenRouter
   - `ASKNEWS_API_KEY`, or both `ASKNEWS_CLIENT_ID` and `ASKNEWS_SECRET`, for current-news research

3. Open **Actions → Initialize private bot state → Run workflow** once. This creates `bot-state` with an empty ledger and refuses to replace an existing branch. Do not delete or reset the branch. Runs fail closed if the branch or ledger is missing. Allow the GitHub Actions token to push ledger commits if you configure branch protection.
4. Enable **Forecast on new tournament questions**. It polls every 20 minutes. GitHub may delay scheduled runs, so this cadence does not guarantee a forecast before a question closes.
5. Review the first run in the Actions log. Do not use live tournament runs as previews or tests.

The ledger records question IDs, paid-call reservations, and actual or reserved costs. The bot records each external side effect before making the request. If a paid call or forecast is interrupted, its reservation remains and the bot does not retry it automatically. These limits are conservative spend ceilings, not a guarantee of the final provider invoice. Set account spend limits where available as a second guard. Set `ASKNEWS_REQUEST_COST_USD` above zero if AskNews can incur charges. Zero is appropriate for free or prepaid access.

Pending requests hold their ceiling against both the current and following UTC day to cover requests that cross midnight. Completed responses release unnecessary next-day holds. An uncertain response retains that hold. OAuth research requests reserve twice the configured request cost to cover a possible authentication replay.

The default caps are `$1` per question, `$10` per UTC day, and `$200` per UTC month. The effective daily limit is recomputed before each reservation. It is the smaller of `$10` and the remaining monthly budget divided by the number of UTC days remaining in the month, including today. Pending request ceilings count against that allowance. Existing ledger spend carries into the monthly limit; do not reset the state branch to restart the budget. At a 10,000-token cached prompt prefix per model, 500 uncached input tokens per call, and the full 4096-token output across four Sonnet calls and two Opus calls, estimated Anthropic model cost is about `$0.42` per question. Without prompt caching, the same six calls cost about `$0.50` at current listed rates. At 7 to 8 questions per day, that is roughly `$3 to $4` per day before optional AskNews charges. These estimates depend on token use and cache hits. The ledger reserves conservative per-call ceilings, so a budget cap can stop forecasts before the provider bill reaches that amount. MiniBench rounds can open around 60 questions in a short burst. The cap can run out during that burst, so the bot may miss questions that close before the next UTC day.

Private repositories can also incur GitHub Actions charges. The workflow polls every 20 minutes and each run can use up to 20 hosted minutes. Check GitHub's current included minutes and billing for your account.

## Models and tournament settings

The default tournament aliases are `fall-futureeval-2026` (official ID `33121`) and `minibench`. The default model mix is four `claude-sonnet-5-5` calls and two `claude-opus-5-5` calls per question. Each call has a 4096-token output limit and low reasoning effort. This is a candidate mix, not a proven winner. No comparative forecasting evaluation has established that it outperforms alternatives.

Anthropic currently lists Sonnet 5.5 at `$2` per million input tokens and `$10` per million output tokens, with cache writes at `$2.50` and cache reads at `$0.10`. Opus 5.5 is `$4` per million input and `$20` per million output, with cache writes at `$5` and cache reads at `$0.20`. Check the [Sonnet model page](https://platform.claude.com/docs/en/models/sonnet-5-5/overview), [Opus model page](https://platform.claude.com/docs/en/models/opus-5-5/whats-new-opus-5-5), and [Anthropic pricing page](https://platform.claude.com/docs/en/about-claude/pricing) before enabling the workflow. Prices can change.

The following arithmetic uses 10,000 shared prefix tokens, 500 uncached suffix tokens per call, and 4,096 output tokens per call, including thinking. Each model pays for its own initial cache write. Later samples of that model reuse it. All prices below are USD per million tokens.

| Model | Calls | Input | 5-minute cache write | Cache read | Output |
| --- | ---: | ---: | ---: | ---: | ---: |
| `claude-sonnet-5-5` | 4 | 2 | 2.50 | 0.10 | 10 |
| `claude-opus-5-5` | 2 | 4 | 5 | 0.20 | 20 |

```text
Sonnet = (10,000*2.50 + 3*10,000*0.10 + 4*500*2 + 4*4,096*10) / 1,000,000
       = $0.19584
Opus   = (10,000*5 + 10,000*0.20 + 2*500*4 + 2*4,096*20) / 1,000,000
       = $0.21984
Total with cache hits = $0.41568 per question
No caching at all     = (4*10,500*2 + 2*10,500*4 + 4*4,096*10 + 2*4,096*20) / 1,000,000
                      = $0.49568 per question
Every request writes a cold cache = $0.53568 per question
```

These are token-count scenarios, not measured average costs. Optional paid AskNews requests are extra. The ledger admits each call using a cold-cache ceiling, so it never relies on getting a cache hit to stay within the cap. Token prices are confirmed from the official pages above; no unconfirmed price fallback is used. `ANTHROPIC_PRICES_JSON` can replace rates as prices change, using a complete object for each overridden model, for example `{"claude-sonnet-5-5":{"input":2,"output":10,"cache_write":2.5,"cache_read":0.1}}`.

The shared question and research context has an explicit 5-minute `cache_control` breakpoint. Sample instructions and timestamps follow it. Calls for each model run in sequence to keep that model's cache warm. Caches are separate for Sonnet and Opus; prompts shorter than the documented 512-token minimum may not cache. The SDK uses the official Anthropic endpoint, with retries disabled. See [Anthropic's prompt caching guide](https://platform.claude.com/docs/en/build-with-claude/prompt-caching).

## Forecasting and tournament rules

When AskNews is enabled, the bot uses dated summaries from recent and archive searches. Each model forecasts from the same evidence, with sample counts set per model. Binary forecasts use the median, capped at 2% and 98%. Multiple choice uses optionwise medians, normalization, and a small uniform mixture. Numeric and discrete forecasts use seven quantiles, the question's linear or logarithmic CDF grid, and pointwise median aggregation. CDF bin limits prevent very narrow distributions. Extremization is disabled. [SOURCES.md](SOURCES.md) explains the evidence and differences from the template.

Only one bot per person or team may win prizes. Each submitted forecast includes a private reasoning comment through the atomic Metaculus API endpoint. Humans may not preview forecasts on open or upcoming tournament questions and then change the bot, choose a rerun, copy forecasts into the site, or provide their own judgment on a particular question. Develop using resolved questions only. The bot permanently skips questions after a failed or uncertain attempt and does not automatically repeat forecasts. Live logs contain status and spend, not forecast values or reasoning.

Use paid or granted API keys. Do not connect the bot to a Claude or ChatGPT consumer subscription. A human must create the human account, identify themselves for payout, complete required surveys, and attend any requested code review. Prize shares depend on the square of each eligible bot's positive summed peer score. This does not establish that this bot will win. See the [official tournament resources](https://www.metaculus.com/notebooks/38928/ai-benchmark-resources/) and [score FAQ source](https://github.com/Metaculus/metaculus/blob/main/front_end/src/app/(main)/help/scores-faq/page.tsx).

## Resolved-question pipeline check

Use **Actions → Forecast on resolved public questions → Run workflow** with comma-separated IDs of already resolved public questions. This checks the pipeline with authenticated read-only Metaculus API requests, Anthropic calls, and the shared budget ledger. It requires `METACULUS_TOKEN` and `ANTHROPIC_API_KEY`. OpenRouter and AskNews credentials are optional. Resolved mode does not publish forecasts.

Resolved questions include their resolution and may also be exposed to current news or hindsight information. This is not an unbiased performance benchmark. Do not run previews, edits, or re-runs on open tournament questions. The upstream open-question test workflow has been removed. For offline checks with stubbed providers, run `python -m unittest discover -s tests -v` from the `bot` directory. This command makes no provider or Metaculus API calls.

## Settings

Set non-secret configuration under **Settings → Secrets and variables → Actions → Variables**. The workflows use the listed defaults when a variable is unset or empty. Do not put API keys or tokens in repository variables. `ANTHROPIC_PRICES_JSON` may override built-in USD-per-million input, output, cache-write, and cache-read prices.

| Variable | Default | Purpose |
| --- | --- | --- |
| `TOURNAMENT_IDS` | `fall-futureeval-2026,minibench` | Tournament aliases or IDs for live mode |
| `FORECAST_MODELS` | `claude-sonnet-5-5,claude-opus-5-5` | Direct Anthropic model IDs; prefix OpenRouter IDs with `openrouter:` (legacy `author/model` IDs are also routed through OpenRouter) |
| `MODEL_SAMPLE_COUNTS` | `4,2` | Calls per model, in the same order as `FORECAST_MODELS` |
| `SAMPLES_PER_MODEL` | unset | An explicit value overrides all model counts when `MODEL_SAMPLE_COUNTS` is unset; custom model lists otherwise use two each |
| `MAX_OUTPUT_TOKENS` | `4096` | Maximum output tokens per model call |
| `QUESTION_BUDGET_USD` | `1` | Maximum reserved cost per question |
| `DAILY_BUDGET_USD` | `10` | Maximum reserved cost per UTC day before monthly limit |
| `MONTHLY_BUDGET_USD` | `200` | Maximum reserved cost per UTC month |
| `ANTHROPIC_PRICES_JSON` | built-in Anthropic rates | Optional JSON map of model prices per million tokens |
| `ASKNEWS_REQUEST_COST_USD` | `0` | Reserved cost per AskNews request when enabled |
| `PROBABILITY_FLOOR` | `0.02` | Minimum forecast probability |

The workflows select `--mode tournament` or `--mode resolved`. Resolved mode requires `RESOLVED_POST_IDS` from its manual input. Both modes require `METACULUS_TOKEN` and `ANTHROPIC_API_KEY`. Resolved mode uses Metaculus only for API reads and cannot publish. The bot runs only in GitHub Actions and does not support local network forecasting. [.env.example](.env.example) lists variable names for reference; the application does not load `.env` files.

No provider keys were available during development. Only fast offline tests with stubbed provider responses have run. The resolved pipeline check, provider inference, Actions state persistence, and live submission endpoint remain unverified against real services.


