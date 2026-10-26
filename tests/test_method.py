"""Offline checks for forecast parsing, aggregation and API constraints."""

import math
import unittest

from futureeval.method import Question, aggregate, parse_prediction


class ForecastMethodTests(unittest.TestCase):
    def question(self, kind="binary", *, lo=0.0, hi=100.0, zero=None,
                 open_lo=False, open_hi=False, count=None, options=None):
        scaling = {"range_min": lo, "range_max": hi, "zero_point": zero}
        if count is not None:
            scaling["inbound_outcome_count"] = count
        question = {
            "id": 123, "title": "A test question", "type": kind,
            "description": "Background", "resolution_criteria": "Resolve carefully",
            "fine_print": "Special cases", "status": "open", "scaling": scaling,
            "open_lower_bound": open_lo, "open_upper_bound": open_hi,
            "scheduled_close_time": "2099-01-01T00:00:00Z",
        }
        if count is not None:
            question["inbound_outcome_count"] = count
        if options is not None:
            question["options"] = list(options)
        return Question.from_post({"id": 456, "title": "A test question", "question": question})

    def quantiles(self, lo=0.0, hi=100.0):
        return {
            "reasoning": "Historical range and current evidence.",
            "quantiles": [
                {"p": p, "value": lo + p * (hi - lo)}
                for p in (.05, .1, .25, .5, .75, .9, .95)
            ],
        }

    def assert_cdf(self, q, cdf):
        self.assertEqual(len(cdf), q.size)
        self.assertTrue(all(math.isfinite(x) and 0 <= x <= 1 for x in cdf))
        self.assertGreaterEqual(cdf[0] + 1e-9, .001 if q.open_lo else 0)
        self.assertLessEqual(cdf[-1] - 1e-9, .999 if q.open_hi else 1)
        if not q.open_lo:
            self.assertEqual(cdf[0], 0)
        if not q.open_hi:
            self.assertEqual(cdf[-1], 1)
        minimum = .01 / (q.size - 1)
        maximum = .2 * 200 / (q.size - 1)
        for left, right in zip(cdf, cdf[1:]):
            self.assertGreaterEqual(right - left + 1e-9, minimum)
            self.assertLessEqual(right - left - 1e-9, maximum)

    def test_post_and_question_ids_stay_distinct(self):
        q = self.question()
        self.assertEqual(q.post_id, 456)
        self.assertEqual(q.id, 123)
        self.assertEqual(q.kind, "binary")

    def test_cdf_sizes_follow_question_metadata(self):
        self.assertEqual(self.question("numeric").size, 201)
        self.assertEqual(self.question("discrete", count=10).size, 11)

    def test_binary_median_then_probability_guardrails(self):
        q = self.question()
        self.assertAlmostEqual(aggregate(q, [.1, .7, .8]), .7)
        self.assertAlmostEqual(aggregate(q, [0, 0, .9]), .02)
        self.assertAlmostEqual(aggregate(q, [.1, 1, 1]), .98)
        self.assertAlmostEqual(aggregate(q, [0, 0, .9], floor=.05), .05)

    def test_binary_parser_rejects_invalid_numbers(self):
        q = self.question()
        self.assertEqual(parse_prediction(q, {"reasoning": "Evidence", "probability": .7}), .7)
        for bad in (True, False, float("nan"), float("inf"), -.1, 1.1, "0.7", None):
            with self.subTest(value=bad), self.assertRaises((ValueError, TypeError)):
                parse_prediction(q, {"reasoning": "Evidence", "probability": bad})

    def test_multiple_choice_preserves_exact_labels_and_normalizes(self):
        q = self.question("multiple_choice", options=("A / exact", "B"))
        result = parse_prediction(q, {
            "reasoning": "Evidence", "probabilities": {"A / exact": .2, "B": .79},
        })
        self.assertEqual(set(result), {"A / exact", "B"})
        self.assertAlmostEqual(sum(result.values()), 1)
        self.assertAlmostEqual(result["A / exact"], .2 / .99)

    def test_multiple_choice_rejects_missing_extra_and_invalid_options(self):
        q = self.question("multiple_choice", options=("A", "B"))
        invalid = [
            {"A": 1}, {"A": .4, "B": .4, "C": .2},
            {"a": .2, "B": .8}, {"A": .1, "B": .8},
            {"A": -.1, "B": 1.1}, {"A": True, "B": 0},
            {"A": float("nan"), "B": .8}, {"A": "0.2", "B": .8},
        ]
        for probabilities in invalid:
            with self.subTest(probabilities=probabilities), self.assertRaises((ValueError, TypeError)):
                parse_prediction(q, {"reasoning": "Evidence", "probabilities": probabilities})

    def test_multiple_choice_optionwise_median_is_normalized(self):
        q = self.question("multiple_choice", options=("A", "B", "C"))
        result = aggregate(q, [
            {"A": .1, "B": .6, "C": .3},
            {"A": .2, "B": .7, "C": .1},
            {"A": .4, "B": .5, "C": .1},
        ], floor=0)
        self.assertAlmostEqual(result["A"], .2 / .9)
        self.assertAlmostEqual(result["B"], .6 / .9)
        self.assertAlmostEqual(result["C"], .1 / .9)

    def test_multiple_choice_floor_keeps_every_option_possible(self):
        q = self.question("multiple_choice", options=("A", "B", "C"))
        result = aggregate(q, [{"A": 1, "B": 0, "C": 0}])
        self.assertAlmostEqual(sum(result.values()), 1)
        self.assertGreaterEqual(min(result.values()), .02)
        self.assertLessEqual(max(result.values()), 1 - (len(q.options) - 1) * .02)

    def test_multiple_choice_zero_median_falls_back_to_uniform(self):
        q = self.question("multiple_choice", options=("A", "B", "C"))
        result = aggregate(q, [
            {"A": 1, "B": 0, "C": 0},
            {"A": 0, "B": 1, "C": 0},
            {"A": 0, "B": 0, "C": 1},
        ])
        for value in result.values():
            self.assertAlmostEqual(value, 1 / 3)

    def test_numeric_closed_bounds_satisfy_api_constraints(self):
        q = self.question("numeric")
        self.assert_cdf(q, parse_prediction(q, self.quantiles()))

    def test_numeric_open_tails_satisfy_api_constraints(self):
        q = self.question("numeric", open_lo=True, open_hi=True)
        self.assert_cdf(q, parse_prediction(q, self.quantiles(-10, 110)))

    def test_numeric_forecasts_far_beyond_open_bounds_keep_valid_tails(self):
        q = self.question("numeric", open_lo=True, open_hi=True)
        for low, high in ((-1100, -1000), (1000, 1100)):
            with self.subTest(low=low, high=high):
                cdf = parse_prediction(q, self.quantiles(low, high))
                self.assert_cdf(q, cdf)
                if high < q.lo:
                    self.assertGreater(cdf[0], .9)
                else:
                    self.assertLess(cdf[-1], .1)

    def test_numeric_tight_distribution_is_capped(self):
        q = self.question("numeric")
        self.assert_cdf(q, parse_prediction(q, self.quantiles(49.999, 50.001)))

    def test_closed_lower_bound_spike_stays_in_first_bucket(self):
        q = self.question("numeric")
        response = self.quantiles()
        for row in response["quantiles"]:
            row["value"] = 0
        cdf = parse_prediction(q, response)
        self.assert_cdf(q, cdf)
        # A spike at the lower bound must not disappear into an artificial lower tail.
        self.assertGreater(cdf[1], .15)

    def test_numeric_log_scaling_uses_log_grid(self):
        q = self.question("numeric", lo=1, hi=1000, zero=0)
        response = self.quantiles()
        for row in response["quantiles"]:
            row["value"] = 1000 ** row["p"]
        cdf = parse_prediction(q, response)
        self.assert_cdf(q, cdf)
        self.assertAlmostEqual(cdf[100], .5, delta=.03)

    def test_discrete_uses_actual_bucket_count(self):
        q = self.question("discrete", lo=-.5, hi=9.5, count=10)
        self.assert_cdf(q, parse_prediction(q, self.quantiles(0, 9)))

    def test_numeric_parser_rejects_invalid_quantiles(self):
        q = self.question("numeric")
        bad_responses = []
        missing = self.quantiles()
        missing["quantiles"].pop()
        bad_responses.append(missing)
        reversed_values = self.quantiles()
        reversed_values["quantiles"][3]["value"] = 1
        bad_responses.append(reversed_values)
        for bad in (True, float("nan"), float("inf"), "50", None):
            response = self.quantiles()
            response["quantiles"][3]["value"] = bad
            bad_responses.append(response)
        for response in bad_responses:
            with self.subTest(response=response), self.assertRaises((ValueError, TypeError)):
                parse_prediction(q, response)

    def test_numeric_aggregate_preserves_valid_distributions(self):
        q = self.question("numeric", open_lo=True, open_hi=True)
        predictions = [parse_prediction(q, self.quantiles(lo, hi))
                       for lo, hi in [(-20, 70), (0, 100), (20, 120)]]
        self.assert_cdf(q, aggregate(q, predictions))


if __name__ == "__main__":
    unittest.main()
