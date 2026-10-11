"""Test suite for ponishabot.

Run with:  python -m unittest discover -s tests -v

Everything here is offline and deterministic: no network, no Selenium, no
ponisha session. The functions under test are the pure ones — the pricing
arithmetic, the scraper's text helpers, the config loader's defaults — which
is where the expensive mistakes lived (a wrong budget read, a price under the
employer's floor, an untagged listing rejected as a total mismatch).
"""

import io
import os
import re
import sys
import unittest
from unittest import mock

# Import the package from the repo root, wherever the tests are launched from.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bs4 import BeautifulSoup

from ponishabot.config import Config
from ponishabot.decision import (HeuristicBidDecision, LLMBidDecision,
                                 _normalize, _parse_llm_json, _validate_plan)
from ponishabot.forex import ForexProvider
from ponishabot.models import BidPlan, MarketComparison, Profile, Project
from ponishabot.scraper import (_coerce_int, _page_text, _strip_carousels,
                                normalize_digits, parse_int)


def project(**kw):
    base = dict(id="1", title="t", url="https://ponisha.ir/project/1")
    base.update(kw)
    return Project(**base)


def profile(**kw):
    base = dict(skills=["پایتون (Python)"], min_budget=2_000_000,
                max_budget=15_000_000)
    base.update(kw)
    return Profile(**base)


class ScraperText(unittest.TestCase):
    """The text helpers, which is where the wrong-budget bug lived."""

    def test_persian_digits_normalized(self):
        self.assertEqual(normalize_digits("۱۲۳۴۵۶۷۸۹۰"), "1234567890")

    def test_parse_int_handles_separators_and_suffix(self):
        self.assertEqual(parse_int("۱۲,۵۰۰,۰۰۰ تومان"), 12_500_000)
        self.assertEqual(parse_int("4500000"), 4_500_000)

    def test_parse_int_returns_none_on_nothing(self):
        for value in (None, "", "بدون بودجه"):
            self.assertIsNone(parse_int(value), value)

    def test_coerce_int_truncates_floats_and_rejects_junk(self):
        self.assertEqual(_coerce_int(4.7), 4)
        self.assertEqual(_coerce_int("4500000"), 4_500_000)
        self.assertIsNone(_coerce_int(None))
        self.assertIsNone(_coerce_int("abc"))

    def test_carousel_removed_but_original_soup_intact(self):
        """The detail page ends in a swiper of ~10 other listings; their budgets
        must not leak in, and the caller's soup must survive for DOM extractors."""
        html = ('<div class="real">بودجه پروژه: ۴۵ میلیون تومان</div>'
                '<div class="mySwiper"><div class="swiper-slide">'
                'بودجه کل پروژه: ۱۲ میلیون تومان</div></div>')
        soup = BeautifulSoup(html, "html.parser")

        cleaned = _strip_carousels(soup).get_text(" ", strip=True)

        self.assertIn("۴۵ میلیون", cleaned)
        self.assertNotIn("۱۲ میلیون", cleaned)
        # the caller still holds an untouched tree
        self.assertIn("mySwiper", str(soup))

    def test_every_carousel_selector_is_stripped(self):
        for sel in (".mySwiper", ".swiper-wrapper", ".swiper-slide"):
            soup = BeautifulSoup(
                f'<div class="keep">keep</div><div class="{sel[1:]}">drop</div>',
                "html.parser")
            text = _strip_carousels(soup).get_text(" ", strip=True)
            self.assertEqual(text, "keep", sel)

    def test_page_text_is_the_stripped_variant(self):
        """fetch_detail reads the page through _page_text, so the strip has to
        live there too — asserting only on _strip_carousels would let it be
        removed from the caller without a single test failing."""
        html = ('<div class="real">بودجه پروژه: ۴۵ میلیون تومان</div>'
                '<div class="mySwiper"><div class="swiper-slide">'
                'بودجه کل پروژه: ۱۲ میلیون تومان</div></div>')
        text = _page_text(BeautifulSoup(html, "html.parser"))

        self.assertIn("۴۵ میلیون", text)
        self.assertNotIn("۱۲ میلیون", text)


class ForexUplift(unittest.TestCase):
    """uplift_factor is pure arithmetic and drives every AI price."""

    def test_rise_above_baseline_passes_through_sensitivity(self):
        # +3.01% move, half of it passed on -> 1.0151
        self.assertEqual(ForexProvider.uplift_factor(235_902, 229_000, 0.5), 1.0151)

    def test_full_sensitivity_passes_the_whole_move(self):
        self.assertEqual(ForexProvider.uplift_factor(235_902, 229_000, 1.0), 1.0301)

    def test_zero_sensitivity_disables_the_uplift(self):
        self.assertEqual(ForexProvider.uplift_factor(235_902, 229_000, 0.0), 1.0)

    def test_weaker_rial_never_discounts_below_the_model_price(self):
        """A rate under the baseline must not drop us under the AI estimate."""
        self.assertEqual(ForexProvider.uplift_factor(180_000, 229_000, 0.5), 1.0)

    def test_missing_inputs_are_a_no_op(self):
        self.assertEqual(ForexProvider.uplift_factor(0, 229_000, 0.5), 1.0)
        self.assertEqual(ForexProvider.uplift_factor(235_902, 0, 0.5), 1.0)


class HeuristicPricing(unittest.TestCase):
    """The worked example from the README, asserted so the docs cannot drift."""

    def test_documented_example_yields_9m_and_15_days(self):
        p = project(budget_min=5_000_000, budget_max=9_000_000, bid_count=4,
                    badges=["فوری"], suggested_days=30)
        plan = HeuristicBidDecision(min_skill_match=0.5).decide(p, profile())
        self.assertTrue(plan.should_bid)
        self.assertEqual(plan.price, 9_000_000)
        self.assertEqual(plan.duration_days, 15)

    def test_price_never_lands_under_the_listing_floor(self):
        """ponisha rejects any proposal below the employer's stated minimum."""
        p = project(budget_min=45_000_000, budget_max=45_000_000, bid_count=30)
        plan = HeuristicBidDecision(min_skill_match=0.5).decide(p, profile())
        self.assertGreaterEqual(plan.price, 45_000_000)

    def test_untagged_listing_is_priced_not_rejected(self):
        """An empty skill list means "unknown", not "no match"."""
        p = project(skills=[], budget_min=5_000_000, budget_max=9_000_000)
        plan = HeuristicBidDecision(min_skill_match=0.5).decide(p, profile())
        self.assertTrue(plan.should_bid, plan.message)

    def test_real_skill_mismatch_is_still_rejected(self):
        p = project(skills=["طراحی لوگو"], budget_min=5_000_000,
                    budget_max=9_000_000)
        plan = HeuristicBidDecision(min_skill_match=0.5).decide(p, profile())
        self.assertFalse(plan.should_bid)

    def test_absurd_budget_is_rejected(self):
        p = project(budget_min=80_000_000, budget_max=90_000_000)
        plan = HeuristicBidDecision(min_skill_match=0.5).decide(p, profile())
        self.assertFalse(plan.should_bid)

    def test_low_competition_raises_the_price(self):
        """Fewer than 5 bids applies the 10% premium: 7M midpoint -> 7.7M.
        A wide range is required — with budget_min == budget_max the final
        clamp back into the budget range would flatten the adjustment."""
        p = project(budget_min=5_000_000, budget_max=9_000_000, bid_count=4)
        plan = HeuristicBidDecision().decide(p, profile())
        self.assertEqual(plan.price, 7_700_000)

    def test_high_competition_discounts(self):
        """15+ bids applies the 15% discount: 7M midpoint -> 5.95M."""
        p = project(budget_min=5_000_000, budget_max=9_000_000, bid_count=20)
        plan = HeuristicBidDecision().decide(p, profile())
        self.assertEqual(plan.price, 5_950_000)

    def test_mid_competition_applies_neither(self):
        """Between 5 and 14 bids is the dead band: the midpoint stands."""
        p = project(budget_min=5_000_000, budget_max=9_000_000, bid_count=10)
        plan = HeuristicBidDecision().decide(p, profile())
        self.assertEqual(plan.price, 7_000_000)


class PricingGuideRanges(unittest.TestCase):
    """The price guide in `profile.about` is read with a regex that only
    understood the Latin 'M'.

    A guide written in Persian — the natural way to write it — matched nothing
    at all, and the estimate quietly fell through to the budget midpoint
    instead. A real profile with five ranges produced zero matches.
    """

    def price_from(self, about, **profile_kw):
        """Estimate from `about` alone.

        The budget window is zeroed by default: `Profile.max_budget` defaults to
        10,000,000, so leaving it would mask every case where the guide found
        nothing and the fallback took over — the first version of these tests
        passed for that reason.
        """
        from ponishabot.decision import HeuristicBidDecision
        from ponishabot.models import Profile
        profile_kw.setdefault("min_budget", 0)
        profile_kw.setdefault("max_budget", 0)
        return HeuristicBidDecision._guide_default_price(
            Profile(skills=["Django"], about=about, **profile_kw))

    def test_persian_unit_is_understood(self):
        got = self.price_from("- CRUD ساده: ۲-۴ میلیون")
        self.assertEqual(got, 3_000_000)

    def test_latin_unit_still_works(self):
        self.assertEqual(self.price_from("- CRUD simple: ۲-۴M"), 3_000_000)

    def test_short_persian_unit_works(self):
        self.assertEqual(self.price_from("- چیزی: ۲-۴ م"), 3_000_000)

    def test_ranges_are_averaged(self):
        got = self.price_from("- a: ۲-۴ میلیون\n- b: ۴-۸ میلیون")
        self.assertEqual(got, 4_500_000)          # (3 + 6) / 2

    def test_a_prose_sentence_is_not_a_range(self):
        """A year span or a version range must not be read as a price."""
        self.assertIsNone(self.price_from("۳.۵ سال فریلنس از ۱۴۰۲ تا ۱۴۰۵"))

    def test_falls_back_to_the_budget_window(self):
        got = self.price_from("no ranges here", min_budget=4_000_000,
                              max_budget=10_000_000)
        self.assertEqual(got, 7_000_000)

    def test_returns_none_with_no_guide_and_no_window(self):
        self.assertIsNone(self.price_from("no ranges here"))

    def test_urgent_badge_adds_20_percent(self):
        """«فوری» is applied on top of whatever competition produced."""
        p = project(budget_min=5_000_000, budget_max=9_000_000, bid_count=4,
                    badges=["فوری"])
        plan = HeuristicBidDecision().decide(p, profile())
        # 7M * 1.10 (low competition) * 1.20 (urgent) = 9.24M, capped at 9M
        self.assertEqual(plan.price, 9_000_000)


class AIPricingAdjustments(unittest.TestCase):
    """_apply_forex_and_clamp and _raise_to_listing_floor are pure given a stub."""

    class StubForex:
        def __init__(self, rate):
            self.rate = rate

        def get_usd_toman(self):
            return self.rate

    def decision(self, rate=235_902, clamp=3.0, target=2.5, enabled=True):
        return LLMBidDecision(
            api_key="x", forex_provider=self.StubForex(rate), forex_enabled=enabled,
            baseline_usd_rate=229_000, forex_sensitivity=0.5,
            karlancer_soft_clamp_enabled=True,
            karlancer_clamp_multiplier=clamp, karlancer_target_multiplier=target)

    def plan(self, price):
        return BidPlan(should_bid=True, price=price, duration_days=21, message="m")

    def test_clamp_does_not_fire_under_the_ceiling(self):
        # average 4M -> ceiling 12M, so 8M survives (uplifted to 8.1M and rounded)
        out = self.decision()._apply_forex_and_clamp(
            self.plan(8_000_000), MarketComparison(avg_budget=4_000_000))
        self.assertEqual(out.price, 8_100_000)

    def test_clamp_fires_above_the_ceiling_and_lands_on_target(self):
        # average 2.6M -> ceiling 7.8M, price 8.12M is over it -> 2.6M * 2.5
        out = self.decision()._apply_forex_and_clamp(
            self.plan(8_000_000), MarketComparison(avg_budget=2_600_000))
        self.assertEqual(out.price, 6_500_000)

    def test_forex_disabled_leaves_the_price_to_rounding(self):
        out = self.decision(enabled=False)._apply_forex_and_clamp(
            self.plan(8_000_000), MarketComparison(avg_budget=4_000_000))
        self.assertEqual(out.price, 8_000_000)

    def test_prices_are_rounded_to_100k(self):
        out = self.decision(rate=0)._apply_forex_and_clamp(self.plan(8_123_456), None)
        self.assertEqual(out.price % 100_000, 0)

    def test_listing_floor_raises_an_undershooting_price(self):
        d = LLMBidDecision(api_key="x")
        floor = project(budget_min=20_000_000, budget_max=25_000_000)
        out = d._raise_to_listing_floor(self.plan(14_100_000), floor)
        self.assertEqual(out.price, 20_000_000)

    def test_listing_floor_leaves_a_sufficient_price_alone(self):
        d = LLMBidDecision(api_key="x")
        out = d._raise_to_listing_floor(self.plan(30_000_000),
                                        project(budget_min=20_000_000))
        self.assertEqual(out.price, 30_000_000)


class LLMResponseParsing(unittest.TestCase):
    """A malformed reply must never raise; the engine falls back instead."""

    def test_plain_json_object(self):
        self.assertEqual(_parse_llm_json('{"should_bid": true, "price": 5}'),
                         {"should_bid": True, "price": 5})

    def test_markdown_fences_are_tolerated(self):
        self.assertEqual(_parse_llm_json('```json\n{"should_bid": false}\n```'),
                         {"should_bid": False})

    def test_garbage_returns_none_rather_than_raising(self):
        self.assertIsNone(_parse_llm_json("garbage"))
        self.assertIsNone(_parse_llm_json('{"should_bid": true,'))

    def test_validate_plan_builds_a_plan(self):
        plan = _validate_plan({"should_bid": True, "price": 7_000_000,
                               "duration_days": 10, "message": "m"},
                              project(budget_min=5_000_000))
        self.assertTrue(plan.should_bid)
        self.assertEqual(plan.price, 7_000_000)

    def test_bid_without_a_price_is_rejected(self):
        self.assertIsNone(_validate_plan({"should_bid": True, "price": None},
                                         project()))

    def test_no_bid_needs_no_price(self):
        plan = _validate_plan({"should_bid": False}, project())
        self.assertFalse(plan.should_bid)


class SkillNormalization(unittest.TestCase):
    def test_case_and_whitespace_insensitive(self):
        self.assertEqual(_normalize("  جنگو (Django)  "), "جنگو (django)")

    def test_empty_is_empty(self):
        self.assertEqual(_normalize(None), "")
        self.assertEqual(_normalize(""), "")


class PassphraseHandling(unittest.TestCase):
    """The pss-at passphrase must never be a constant in the source, and a
    missing one must fail loudly rather than encrypt with an empty key."""

    def setUp(self):
        from ponishabot import auth
        self.auth = auth
        self.saved = os.environ.pop(auth.PASSPHRASE_ENV, None)

    def tearDown(self):
        if self.saved is not None:
            os.environ[self.auth.PASSPHRASE_ENV] = self.saved
        else:
            os.environ.pop(self.auth.PASSPHRASE_ENV, None)

    def test_no_hardcoded_passphrase_in_the_module(self):
        """A literal default would be readable by anyone who can see the repo.

        Checks the shape of a constant rather than a specific value: naming the
        old string here would put it back into the repository this test exists
        to keep it out of.
        """
        source = open(self.auth.__file__, encoding="utf-8").read()
        self.assertNotIn("_DEFAULT_TOKEN_PASSPHRASE", source)
        # A key would be assigned to something named as a secret, and would not
        # be the env var's own name. Matching that shape finds a reintroduced
        # default without naming the old value in this file.
        suspicious = [
            line for line in re.findall(
                r"^\s*([A-Za-z_]\w*)\s*=\s*[\"']([^\"']+)[\"']", source, re.M)
            if "PASSPHRASE" in line[0].upper()
            and line[1] != self.auth.PASSPHRASE_ENV
        ]
        self.assertEqual(suspicious, [], f"literal passphrase present: {suspicious}")

    def test_env_var_supplies_the_passphrase(self):
        os.environ[self.auth.PASSPHRASE_ENV] = "from-env"
        self.assertEqual(self.auth.PonishaAuth()._TOKEN_PASSPHRASE, "from-env")

    def test_blank_env_var_is_not_a_passphrase(self):
        """systemd passes `VAR=` (empty); an empty key produced a cookie the
        site rejected, which aborted every bid."""
        os.environ[self.auth.PASSPHRASE_ENV] = "   "
        self.assertEqual(self.auth.PonishaAuth()._TOKEN_PASSPHRASE, "")

    def test_absent_configuration_yields_empty_not_a_default(self):
        self.assertEqual(self.auth.PonishaAuth()._TOKEN_PASSPHRASE, "")

    def test_building_a_cookie_without_a_passphrase_raises(self):
        """Rather than silently derive a key from an empty string."""
        with self.assertRaises(ValueError):
            self.auth.PonishaAuth()._build_pss_at("a.b.c")

    def test_cookie_building_returns_nothing_when_unconfigured(self):
        """_token_cookies swallows the ValueError into an empty list, which the
        caller reads as 'cookie path unavailable'."""
        self.assertEqual(self.auth.PonishaAuth()._token_cookies(), [])


class PanelLogHandling(unittest.TestCase):
    """The terminal panel's log filter. Pure string work, so it is testable
    without a TTY — which matters because the panel's job is partly to hide
    noise the console used to dump raw."""

    def setUp(self):
        from ponishabot.tui import bridge
        self.bridge = bridge

    # A real excerpt: the run that motivated the filter had this line nine
    # times in a row, burying everything else.
    NOISY = [
        "2026-10-09 16:32:09,379 - WARNING - Forex fetch failed: HTTPSConnectionPool(host='api.wallex.ir')",
    ] * 9 + [
        "2026-10-09 16:32:10,000 - INFO - Karlancer market: 48 projects, avg=5806666",
        "2026-10-09 16:32:11,000 - ERROR - Error in monitor cycle: boom",
    ]

    def test_classify_splits_level_time_message(self):
        lvl, ts, msg = self.bridge.classify_line(
            "2026-10-09 16:32:10,000 - INFO - hello")
        self.assertEqual(lvl, "INFO")
        self.assertEqual(ts, "16:32:10")
        self.assertEqual(msg, "hello")

    def test_classify_survives_a_non_log_line(self):
        """Tracebacks and banners have no level; they must not crash the panel."""
        lvl, ts, msg = self.bridge.classify_line("  File \"x.py\", line 3")
        self.assertEqual(lvl, "info")
        self.assertEqual(ts, "")
        self.assertIn("line 3", msg)

    def test_noise_is_recognised(self):
        self.assertTrue(self.bridge.is_noise("Forex fetch failed: timeout"))
        self.assertFalse(self.bridge.is_noise("Karlancer market: 48 projects"))

    def test_noise_hidden_by_default_and_shown_on_request(self):
        default = self.bridge.filter_lines(self.NOISY, level="info")
        self.assertFalse(any("Forex fetch failed" in l for l in default))
        shown = self.bridge.filter_lines(self.NOISY, level="info", show_noise=True)
        self.assertTrue(any("Forex fetch failed" in l for l in shown))

    def test_important_keeps_errors_and_drops_plain_info(self):
        lines = self.NOISY + [
            "2026-10-09 16:32:12,000 - INFO - Decision engine: AI (respone) with heuristic fallback",
        ]
        kept = self.bridge.filter_lines(lines, level="important")
        self.assertTrue(any("ERROR" in l for l in kept))
        self.assertFalse(any("Decision engine" in l for l in kept))

    def test_info_keeps_info_lines(self):
        kept = self.bridge.filter_lines(self.NOISY, level="info")
        self.assertTrue(any("Karlancer market" in l for l in kept))

    def test_important_still_shows_bot_activity(self):
        """A healthy run is mostly INFO. Keeping only WARNING+ would leave the
        default view nearly empty while the bot was working — and the noisiest
        lines in this project are WARNINGs, so severity makes a poor filter."""
        lines = [
            "2026-10-09 16:32:10,000 - INFO - AI: bid 7,700,000 Toman / 15 days — matched",
            "2026-10-09 16:32:11,000 - INFO - Rejected: طراحی لوگو (skill overlap 20% < 50%)",
            "2026-10-09 16:32:12,000 - INFO - Monitor started (poll interval: 60s)",
        ]
        kept = self.bridge.filter_lines(lines, level="important")
        self.assertEqual(len(kept), 3, "event lines must survive the default view")

    def test_important_still_drops_uninteresting_info(self):
        lines = [
            "2026-10-09 16:32:10,000 - INFO - Decision engine: AI (respone) with heuristic fallback",
            "2026-10-09 16:32:10,000 - INFO - BotApp singleton created (web GUI)",
        ]
        self.assertEqual(self.bridge.filter_lines(lines, level="important"), [])

    def test_event_classification(self):
        self.assertTrue(self.bridge.is_event("AI: bid 5,000,000 Toman"))
        self.assertTrue(self.bridge.is_event("Monitor stopped"))
        self.assertFalse(self.bridge.is_event("Decision engine: AI (respone)"))

    def test_repeats_collapse_into_one_row_with_a_count(self):
        """The whole point: nine identical failures become one row."""
        collapsed = self.bridge.collapse_repeats(self.NOISY)
        noise_rows = [c for c in collapsed if "Forex fetch failed" in c[0]]
        self.assertEqual(len(noise_rows), 1)
        self.assertEqual(noise_rows[0][1], 9)

    def test_distinct_lines_are_not_merged(self):
        collapsed = self.bridge.collapse_repeats([
            "2026-10-09 16:32:10,000 - INFO - a",
            "2026-10-09 16:32:11,000 - INFO - b",
        ])
        self.assertEqual([c[1] for c in collapsed], [1, 1])

    def test_repeated_message_with_different_timestamps_still_collapses(self):
        """Comparing raw lines would never match: the timestamps differ. The
        run being counted is exactly this shape."""
        collapsed = self.bridge.collapse_repeats([
            "2026-10-09 16:32:09,379 - WARNING - same failure",
            "2026-10-09 16:32:14,474 - WARNING - same failure",
        ])
        self.assertEqual(len(collapsed), 1)
        self.assertEqual(collapsed[0][1], 2)


class PanelFormatting(unittest.TestCase):
    def setUp(self):
        from ponishabot.tui import bridge
        self.bridge = bridge

    def test_money_groups_digits(self):
        self.assertEqual(self.bridge.fmt_money(7_700_000), "7,700,000")

    def test_money_handles_unknown(self):
        for v in (None, "", "abc"):
            self.assertEqual(self.bridge.fmt_money(v), "—", v)

    def test_duration_scales(self):
        self.assertEqual(self.bridge.fmt_duration(8), "8s")
        self.assertEqual(self.bridge.fmt_duration(760), "12m 40s")
        self.assertEqual(self.bridge.fmt_duration(11_220), "3h 07m")

    def test_duration_handles_none_and_negative(self):
        self.assertEqual(self.bridge.fmt_duration(None), "—")
        self.assertEqual(self.bridge.fmt_duration(-5), "—")

    def test_layout_switches_on_width(self):
        self.assertEqual(self.bridge.layout_for_width(120), "wide")
        self.assertEqual(self.bridge.layout_for_width(60), "narrow")

    def test_strip_ansi_removes_escapes(self):
        self.assertEqual(self.bridge.strip_ansi("\x1b[31mred\x1b[0m"), "red")

    def test_truncate_measures_display_cells_not_characters(self):
        """A Persian line is not as wide as len() suggests, so clipping by
        character count would cut the wrong amount."""
        long = "این یک خط بسیار طولانی فارسی است که باید کوتاه شود"
        out = self.bridge.truncate_width(long, 20)
        from rich.cells import cell_len
        self.assertLessEqual(cell_len(out), 20)
        self.assertTrue(out.endswith("…"))

    def test_truncate_leaves_a_short_line_alone(self):
        self.assertEqual(self.bridge.truncate_width("کوتاه", 40), "کوتاه")

    def test_money_uses_an_ascii_comma(self):
        """The interface is English, so the grouping separator is a comma —
        not the Persian one (U+066C) it used to be."""
        self.assertNotIn("٬", self.bridge.fmt_money(7_700_000))

    def test_status_rows_reflect_the_snapshot(self):
        rows = dict(self.bridge.status_rows({
            "running": True, "quota": 60, "matches": 3, "bids": 11,
            "ai_enabled": True, "has_cookies": True, "refresh_interval": 60,
            "uptime": 3725}))
        self.assertEqual(rows["status"], "RUNNING")
        self.assertEqual(rows["quota"], "60")
        self.assertEqual(rows["matches"], "3")
        self.assertEqual(rows["uptime"], "1h 02m")

    def test_status_rows_say_bidding_before_running(self):
        """Bidding is the more specific state and must win the label."""
        rows = dict(self.bridge.status_rows({"running": True, "bidding": True}))
        self.assertEqual(rows["status"], "BIDDING")


class TerminalSingleton(unittest.TestCase):
    """Regression guard for the trap that made the panel report on a BotApp it
    never ran: main.py built its own instance while botstate held another, so
    status_snapshot() said "stopped" with no quota during a live run.

    `get_app()` reads config.yaml, which is gitignored and therefore absent in
    a fresh clone and in CI. These point it at a temp file and reset the
    singleton afterwards, so the suite passes anywhere instead of only on a
    machine that happens to have a config.
    """

    def setUp(self):
        import tempfile
        from ponishabot import botstate
        self.botstate = botstate
        self._saved_app = botstate._app
        botstate._app = None                      # force a fresh build
        self._tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".yaml", delete=False, encoding="utf-8")
        self._tmp.write("bid:\n  dry_run: true\nsearch:\n  skills: []\n")
        self._tmp.close()
        self._patch = _patch_config_path(self._tmp.name)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.botstate._app = self._saved_app
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def test_main_goes_through_get_app(self):
        source = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
        self.assertIn("botstate.get_app()", source)
        self.assertNotIn("BotApp(cfg)", source,
                         "main.py must not construct its own BotApp")

    def test_get_app_is_a_singleton(self):
        first = self.botstate.get_app()
        self.assertIs(self.botstate.get_app(), first)

    def test_the_singleton_is_what_status_reports_on(self):
        """The bug: a second instance made status_snapshot() describe an app
        the panel was not running, so it always read `running: False`."""
        app = self.botstate.get_app()
        self.assertIs(self.botstate._app, app)
        snap = self.botstate.status_snapshot()
        self.assertIn("running", snap)


def _patch_config_path(path: str):
    """Point BOTH `ConfigLoader.load` and `.save` at a temp file for one test.

    `save` is included because leaving it out destroyed the operator's real
    config.yaml: a test pressed Ctrl+S in the settings screen, `save` wrote to
    the live file, and every value — skills, tokens, profile — was replaced by
    the test's defaults. Nothing in the suite could have caught that, because
    the test passed; it was the file on disk that was wrong.

    Replacing `paths.CONFIG_PATH` does not work: `load` declares
    `path: str = paths.CONFIG_PATH` as a default argument and Python binds
    defaults once at def time. Replacing `__defaults__` worked until a second
    parameter was added and the tuple had to grow with it — a wrapper taking
    *args/**kwargs cannot go stale that way.
    """
    from ponishabot import config
    real_load = config.ConfigLoader.load
    real_save = config.ConfigLoader.save

    def load(*args, **kwargs):
        kwargs.setdefault("path", path)
        return real_load(*args, **kwargs)

    def save(cfg, *args, **kwargs):
        kwargs.setdefault("path", path)
        return real_save(cfg, *args, **kwargs)

    return mock.patch.multiple(
        config.ConfigLoader,
        load=staticmethod(load),
        save=staticmethod(save),
    )


def _textual_available() -> bool:
    try:
        import textual  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(_textual_available(), "textual is not installed")
class TuiNavigation(unittest.TestCase):
    """Drives the real app through Textual's test harness.

    This is the part a pure-function test cannot reach: whether keys actually
    arrive, whether focus swallows them, and whether Tab really cycles. Two
    bugs surfaced here that no amount of unit testing would have caught — every
    screen compared equal because `Screen.name` defaults to None, and a focused
    Input ate the shortcut keys.
    """

    def setUp(self):
        import tempfile
        from ponishabot import config, botstate
        self.botstate = botstate
        self._saved_app = botstate._app
        botstate._app = None
        self._tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".yaml", delete=False, encoding="utf-8")
        self._tmp.write("bid:\n  dry_run: true\nsearch:\n  skills: []\n")
        self._tmp.close()
        self._patch = _patch_config_path(self._tmp.name)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.botstate._app = self._saved_app
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def _run(self, coro):
        import asyncio
        return asyncio.run(coro)

    def _build(self):
        from ponishabot.tui.app import PonishaTUI
        from ponishabot.tui.screens import ALL_SCREENS
        PonishaTUI.SCREEN_CLASSES = list(ALL_SCREENS)
        return PonishaTUI(self.botstate.get_app())

    def test_menu_rows_show_their_shortcut_numbers(self):
        """The menu opened with every number missing.

        `[1]` is valid Rich markup naming an unknown style, and Rich renders
        nothing at all for it. This checks the rendered text, not the source:
        an earlier version of this test scanned the file with a regex and
        passed against the broken code, which is worse than having no test.
        """
        async def scenario():
            app = self._build()
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                from textual.widgets import Static
                rows = []
                for w in app.screen.query(Static):
                    content = getattr(w, "content", None)
                    if content is None:
                        continue
                    try:
                        from rich.text import Text
                        rows.append(content.plain if isinstance(content, Text)
                                    else Text.from_markup(str(content)).plain)
                    except Exception:
                        rows.append(str(content))
                await pilot.press("q")
                return rows

        rows = self._run(scenario())
        joined = "\n".join(rows)
        for i in range(1, 6):
            self.assertIn(f"[{i}]", joined,
                          f"menu shortcut [{i}] missing; rendered:\n{joined}")

    def test_log_screen_shows_its_noise_state(self):
        """`[hidden]` is also unknown markup, and the label vanished the same
        way. Checks the rendered header rather than the source."""
        async def scenario():
            app = self._build()
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                await pilot.press("5")           # menu -> log
                await pilot.pause()
                from textual.widgets import Static
                w = app.screen.query_one("#log-head", Static)
                content = getattr(w, "content", "")
                from rich.text import Text
                plain = (content.plain if isinstance(content, Text)
                         else Text.from_markup(str(content)).plain)
                await pilot.press("q")
                return plain

        header = self._run(scenario())
        self.assertIn("hidden", header, f"noise state missing: {header!r}")
        self.assertIn("level", header)

    def test_menu_is_the_landing_screen(self):
        """The app opens on the menu, not straight into the dashboard."""
        async def scenario():
            app = self._build()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                name = type(app.screen).__name__
                await pilot.press("q")
                return name

        self.assertEqual(self._run(scenario()), "MenuScreen")

    def test_menu_opens_each_screen_by_number(self):
        """1-5 jump straight to a screen. Unlike app-level bindings, these work
        because the menu has no text field to swallow the digit."""
        async def scenario():
            app = self._build()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                reached = {}
                for key, expected in [("1", "DashboardScreen"), ("2", "ProjectsScreen"),
                                      ("3", "SettingsScreen"), ("4", "LoginScreen"),
                                      ("5", "LogScreen")]:
                    await pilot.press(key)
                    await pilot.pause()
                    reached[key] = type(app.screen).__name__
                    await pilot.press("escape")      # back to the menu
                    await pilot.pause()
                await pilot.press("q")
                return reached

        reached = self._run(scenario())
        for key, expected in [("1", "DashboardScreen"), ("2", "ProjectsScreen"),
                              ("3", "SettingsScreen"), ("4", "LoginScreen"),
                              ("5", "LogScreen")]:
            self.assertEqual(reached[key], expected, f"key {key}")

    def test_menu_enter_opens_the_highlighted_item(self):
        async def scenario():
            app = self._build()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await pilot.press("j")               # highlight Projects
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                name = type(app.screen).__name__
                await pilot.press("q")
                return name

        self.assertEqual(self._run(scenario()), "ProjectsScreen")

    def test_monitor_does_not_start_on_its_own(self):
        """Launching the app must not begin bidding before the operator has
        looked at anything: with dry_run off, that used to fire immediately."""
        async def scenario():
            app = self._build()
            started = []
            app.start_monitor = lambda: started.append(1)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await pilot.press("q")
                return started

        self.assertEqual(self._run(scenario()), [],
                         "on_mount must not start the monitor")

    def test_ctrl_arrows_cycle_through_every_screen_and_wrap(self):
        """Ctrl+arrows change screens, and keep working while a text field has
        focus — which is why they are not `]`/`[`. A printable key never
        reaches the app at all when an Input is focused."""
        async def scenario():
            app = self._build()
            from ponishabot.tui.screens import ALL_SCREENS
            count = len(ALL_SCREENS)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                seen = []
                for _ in range(count):
                    seen.append(type(app.screen).__name__)
                    await pilot.press("ctrl+right")
                    await pilot.pause()
                wrapped = type(app.screen).__name__
                await pilot.press("q")
                return seen, wrapped, count

        seen, wrapped, count = self._run(scenario())
        self.assertEqual(len(set(seen)), count, f"screens not distinct: {seen}")
        self.assertEqual(wrapped, seen[0], "the cycle did not wrap around")

    def test_screen_change_works_with_a_field_focused(self):
        """The regression: on the login screen focus sits in a text field, and
        a printable navigation key was swallowed there, leaving no way out."""
        async def scenario():
            app = self._build()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await pilot.press("4")               # menu -> login
                await pilot.pause()
                focused = type(app.focused).__name__
                await pilot.press("ctrl+right")
                await pilot.pause()
                after = type(app.screen).__name__
                await pilot.press("q")
                return focused, after

        focused, after = self._run(scenario())
        self.assertEqual(focused, "Input", "the test needs a field to be focused")
        self.assertNotEqual(after, "LoginScreen",
                            "ctrl+right did not leave the login screen")

    def test_ctrl_left_goes_backwards(self):
        async def scenario():
            app = self._build()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await pilot.press("ctrl+right")
                await pilot.pause()
                forward = type(app.screen).__name__
                await pilot.press("ctrl+left")
                await pilot.pause()
                back = type(app.screen).__name__
                await pilot.press("q")
                return forward, back

        forward, back = self._run(scenario())
        self.assertNotEqual(forward, back)

    def test_settings_sections_cover_every_field(self):
        """The section tabs must hold the same settings the dashboard does,
        with nothing dropped in the regrouping."""
        from ponishabot.tui.screens import SettingsScreen
        counts = {name: len(fields) for name, fields in SettingsScreen.SECTIONS}
        self.assertEqual(counts, {
            "Controls": 5, "Telegram": 2, "Search & budget": 8, "Profile": 4,
            "AI": 9, "Pricing": 5, "Forex & clamp": 7,
        })
        self.assertEqual(sum(counts.values()), 40)

    def test_settings_tab_cycles_sections_not_screens(self):
        """Inside Settings, Tab moves between the sections."""
        async def scenario():
            app = self._build()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await pilot.press("3")               # menu -> settings
                await pilot.pause()
                names = []
                for _ in range(8):                   # one more than the count
                    scr = app.screen
                    self.assertEqual(type(scr).__name__, "SettingsScreen",
                                     "Tab left the settings screen")
                    names.append(scr.SECTIONS[scr._section][0])
                    await pilot.press("tab")
                    await pilot.pause()
                await pilot.press("q")
                return names

        names = self._run(scenario())
        self.assertEqual(len(set(names)), 7, f"sections: {names}")
        self.assertEqual(names[0], names[7], "the sections did not wrap")

    def test_dashboard_has_start_and_stop_buttons(self):
        """The keys alone were not discoverable: nothing on screen said `s`
        existed, so starting the bot meant reading the source."""
        from textual.widgets import Button

        async def scenario():
            app = self._build()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await pilot.press("1")               # menu -> dashboard
                await pilot.pause()
                ids = {b.id for b in app.screen.query(Button)}
                labels = {str(b.label) for b in app.screen.query(Button)}
                await pilot.press("q")
                return ids, labels

        ids, labels = self._run(scenario())
        self.assertIn("btn-start", ids)
        self.assertIn("btn-stop", ids)
        self.assertIn("Start", labels)
        self.assertIn("Stop", labels)

    def test_start_button_starts_the_bot(self):
        from textual.widgets import Button

        async def scenario():
            app = self._build()
            started = []
            app.start_monitor = lambda: started.append(1)
            # A monitor is required for the button to be willing to start.
            app.bot.monitor = object()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await pilot.press("1")
                await pilot.pause()
                # Activated directly rather than via Tab: Tab cycles screens
                # here, so pressing it walked off the dashboard.
                app.screen.query_one("#btn-start", Button).press()
                await pilot.pause()
                await pilot.press("q")
                return started

        self.assertEqual(self._run(scenario()), [1],
                         "the Start button did not start the monitor")

    def test_stop_button_is_disabled_until_running(self):
        """The buttons track state, so it is visible at a glance whether the
        bot is up, and Start cannot be pressed twice."""
        from textual.widgets import Button

        async def scenario():
            app = self._build()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await pilot.press("1")
                await pilot.pause()
                start = app.screen.query_one("#btn-start", Button)
                stop = app.screen.query_one("#btn-stop", Button)
                await pilot.press("q")
                return start.disabled, stop.disabled

        start_disabled, stop_disabled = self._run(scenario())
        self.assertFalse(start_disabled, "Start should be available when stopped")
        self.assertTrue(stop_disabled, "Stop should be disabled when stopped")

    def test_footer_shows_the_key_legend(self):
        """Without it, `s` and the rest were invisible."""
        from textual.widgets import Footer

        async def scenario():
            app = self._build()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                has_footer = bool(app.query(Footer))
                # `ActiveBinding` wraps the Binding; the key is on `.binding`.
                keys = {ab.binding.key
                        for ab in app.screen.active_bindings.values()}
                await pilot.press("q")
                return has_footer, keys

        has_footer, keys = self._run(scenario())
        self.assertTrue(has_footer, "no Footer is composed, so no key legend")
        self.assertIn("q", keys)

    def test_label_is_level_with_its_field(self):
        """Label and field occupy the same rows on screen.

        Measured with `region`, not `size`: `size.height` is the content box and
        reports 1 for a field whose border occupies three rows, so comparing it
        against the label's compared two different things — the earlier version
        of this test did exactly that and passed against misaligned rows.

        Both field kinds are checked because they differ: an Input's box is
        three rows via its border, while a Switch reports a content height of 1
        and is centred in its row instead.
        """
        async def scenario():
            app = self._build()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await pilot.press("3")               # menu -> settings
                await pilot.pause()
                rows = {}
                for field_id in ("f-proposal_template", "f-dry_run"):
                    field = app.screen.query_one(f"#{field_id}")
                    label = next(c for c in field.parent.children
                                 if type(c).__name__ == "Label")
                    rows[field_id] = (label.region, field.region)
                await pilot.press("q")
                return rows

        rows = self._run(scenario())
        for field_id, (label, field) in rows.items():
            # The label's rows must sit inside the field's rows, not above them.
            self.assertTrue(
                field.y <= label.y < field.y + field.height,
                f"{field_id}: label starts at y={label.y} "
                f"(h={label.height}), field occupies y={field.y}.."
                f"{field.y + field.height - 1}")
        # The label is the taller of the two by design, so a regression that
        # dropped it back to one row would show up here.
        for field_id, (label, _field) in rows.items():
            self.assertGreater(
                label.height, 1,
                f"{field_id}: the label is one row, so it is not stretched "
                f"to its field")

    def test_unset_text_field_shows_empty_not_none(self):
        """`None` is unset, not the four-character string "None".

        `user_data_dir` displayed 'None' in its box, which then read back as a
        path if the form was ever saved unchanged.
        """
        from textual.widgets import Input

        async def scenario():
            app = self._build()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await pilot.press("3")
                await pilot.pause()
                value = app.screen.query_one("#f-user_data_dir", Input).value
                await pilot.press("q")
                return value

        self.assertNotIn(self._run(scenario()), ("None", "none"),
                         "an unset field rendered as the text 'None'")

    def test_proposal_file_and_profile_dir_are_saved(self):
        """Both are strings with no BotApp setter, so they were edited, shown as
        saved, and silently dropped — the dashboard writes both."""
        from textual.widgets import Input

        async def scenario():
            app = self._build()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await pilot.press("3")
                await pilot.pause()
                app.screen.query_one("#f-proposal_template", Input).value = "mine.txt"
                app.screen.query_one("#f-user_data_dir", Input).value = "/tmp/prof"
                await pilot.press("ctrl+s")
                await pilot.pause()
                out = (app.bot.cfg.proposal_template, app.bot.cfg.user_data_dir)
                await pilot.press("q")
                return out

        proposal, profile_dir = self._run(scenario())
        self.assertEqual(proposal, "mine.txt",
                         "the proposal file was not saved")
        self.assertEqual(profile_dir, "/tmp/prof",
                         "the Chrome profile dir was not saved")

    def test_clearing_the_profile_dir_stores_null_not_empty(self):
        """Empty means unset: "" would be read as a real directory path."""
        from textual.widgets import Input

        async def scenario():
            app = self._build()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await pilot.press("3")
                await pilot.pause()
                inp = app.screen.query_one("#f-user_data_dir", Input)
                inp.value = "  "
                await pilot.press("ctrl+s")
                await pilot.pause()
                value = app.bot.cfg.user_data_dir
                await pilot.press("q")
                return value

        self.assertIsNone(self._run(scenario()),
                          "a blank profile dir was stored as an empty string")

    def test_login_has_a_visible_code_field(self):
        """The code box must be on screen from the start.

        It used to be hidden until `request_otp` succeeded, so there was no way
        to type a code in — and with an empty box, Enter on the mobile field
        did nothing and the screen looked broken.
        """
        from textual.widgets import Input

        async def scenario():
            app = self._build()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await pilot.press("4")               # menu -> login
                await pilot.pause()
                otp = app.screen.query_one("#otp", Input)
                await pilot.press("q")
                return otp.display, otp.size.height, otp.can_focus

        display, height, focusable = self._run(scenario())
        self.assertTrue(display, "the code field is hidden")
        self.assertGreater(height, 0, "the code field has no height")
        self.assertTrue(focusable, "the code field cannot be focused")

    def test_login_rejects_an_empty_or_bad_mobile(self):
        """Enter with nothing typed must say so, not sit there."""
        from textual.widgets import Static

        async def scenario():
            app = self._build()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await pilot.press("4")
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                note = app.screen.query_one("#note", Static)
                content = getattr(note, "content", "")
                # `content` is already markup; rendering it again as markup
                # returned an empty string and made this test pass on nothing.
                text = content.plain if hasattr(content, "plain") else str(content)
                await pilot.press("q")
                return text

        self.assertIn("mobile", self._run(scenario()).lower(),
                      "an empty submit gave no guidance")

    def test_settings_has_connection_test_buttons(self):
        """Test AI and Test Telegram exist in the dashboard; without them here
        the only way to check credentials was to leave the terminal."""
        from textual.widgets import Button

        async def scenario():
            app = self._build()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await pilot.press("3")               # menu -> settings
                await pilot.pause()
                found = {}
                for i, (name, _fields) in enumerate(app.screen.SECTIONS):
                    ids = {b.id for b in app.screen.query_one(f"#sec-{i}").query(Button)}
                    if ids:
                        found[name] = ids
                await pilot.press("q")
                return found

        found = self._run(scenario())
        self.assertIn("btn-test-tg", found.get("Telegram", set()),
                      "no Telegram test button in the Telegram section")
        self.assertIn("btn-test-ai", found.get("AI", set()),
                      "no AI test button in the AI section")

    def test_telegram_test_reads_the_form_not_the_saved_config(self):
        """Typing new credentials and pressing Test must test those, or the
        button answers a question the operator did not ask."""
        from textual.widgets import Button, Input

        async def scenario():
            app = self._build()
            sent = {}

            class FakeNotifier:
                def __init__(self, token, chat_id):
                    sent["token"] = token
                    sent["chat_id"] = chat_id

                def send(self, *a, **k):
                    return True

            import ponishabot.notifier as notifier
            real = notifier.TelegramNotifier
            notifier.TelegramNotifier = FakeNotifier
            try:
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    await pilot.press("3")
                    await pilot.pause()
                    # Type fresh values into the form.
                    app.screen._inputs["telegram_token"].value = "TYPED-TOKEN"
                    app.screen._inputs["telegram_chat_id"].value = "TYPED-CHAT"
                    app.screen.query_one("#btn-test-tg", Button).press()
                    await pilot.press("q")
            finally:
                notifier.TelegramNotifier = real
            return sent

        sent = self._run(scenario())
        self.assertEqual(sent.get("token"), "TYPED-TOKEN",
                         "the test used the saved token, not the typed one")

    def test_connection_test_reports_its_result(self):
        """The answer must replace "testing…", not leave it on screen forever.

        `call_from_thread` lives on the App, not on a Screen. Nine call sites
        used `self.call_from_thread`, which raised AttributeError inside the
        worker thread — so the note stayed on "sending…" and the real outcome
        was never shown. The thread died silently; nothing surfaced it.
        """
        import asyncio
        from textual.widgets import Button, Static

        async def scenario():
            app = self._build()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await pilot.press("3")
                await pilot.pause()
                # Point the form at something that cannot work, so the outcome
                # is a failure — the point is that *an* outcome arrives.
                app.screen._inputs["telegram_token"].value = "BOGUS-TOKEN"
                app.screen._inputs["telegram_chat_id"].value = "1"
                app.screen.query_one("#btn-test-tg", Button).press()
                await pilot.pause()

                note = app.screen.query_one("#notice", Static)
                first = str(getattr(note, "content", ""))
                # Let the worker finish; no blocking sleep, or the event loop
                # that has to deliver the result never runs.
                for _ in range(30):
                    await asyncio.sleep(0.2)
                    await pilot.pause()
                    current = str(getattr(note, "content", ""))
                    if "sending" not in current:
                        break
                await pilot.press("q")
                return first, current

        first, final = self._run(scenario())
        self.assertIn("sending", first, "the button did not start a test")
        self.assertNotIn("sending", final,
                         f"the test never reported a result; still: {final!r}")

    def test_no_screen_calls_call_from_thread_on_itself(self):
        """Guards the whole class of bug, not just the two buttons.

        `Screen` has no `call_from_thread`; it belongs to `App`. Any new call
        site using `self.call_from_thread` fails the same silent way.
        """
        from ponishabot.tui import screens as screens_mod
        source = open(screens_mod.__file__, encoding="utf-8").read()
        offenders = [f"line {n}"
                     for n, line in enumerate(source.splitlines(), 1)
                     if "self.call_from_thread" in line]
        self.assertEqual(offenders, [],
                         f"call_from_thread on a Screen raises at runtime: {offenders}")

    def test_saving_never_touches_the_real_config(self):
        """The test harness must redirect `save` as well as `load`.

        Leaving `save` unpatched destroyed the operator's config.yaml: a test
        pressed Ctrl+S, the settings screen called the real `ConfigLoader.save`,
        and every value — skills, Telegram token, AI key, profile — was
        replaced with the test's defaults. The suite passed; the file on disk
        was wrong.

        A missing config.yaml is a valid starting state, not a failure: it is
        gitignored, so it is absent in CI and in a fresh clone. The first
        version of this test asserted the file existed and failed on every CI
        runner for that reason.
        """
        from textual.widgets import Input
        from ponishabot import paths

        # A value distinctive enough to find if it leaks, written to a field
        # that a save would include.
        marker = "LEAK-CANARY-9152"

        async def scenario():
            app = self._build()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await pilot.press("3")               # menu -> settings
                await pilot.pause()
                app.screen.query_one("#f-profile_name", Input).value = marker
                await pilot.press("ctrl+s")
                await pilot.pause()
                await pilot.press("q")

        real_path = paths.CONFIG_PATH
        existed = os.path.exists(real_path)
        before = open(real_path, encoding="utf-8").read() if existed else None
        self._run(scenario())

        if not existed:
            # Never present on a fresh clone or CI. A test creating it would be
            # just as much a leak as overwriting one, so that is the check.
            self.assertFalse(
                os.path.exists(real_path),
                "a test created the real config.yaml")
            return

        after = open(real_path, encoding="utf-8").read()
        self.assertNotIn(marker, after,
                         "a test value reached the real config.yaml")
        self.assertEqual(before, after,
                         "the real config.yaml was rewritten by a test")

    def test_settings_saves_fields_from_a_hidden_section(self):
        """Ctrl+S must read every section, not only the visible one.

        A field the user edited and then tabbed away from still holds the new
        value; saving only the active tab would report success while silently
        discarding it.
        """
        async def scenario():
            from textual.widgets import Input
            app = self._build()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await pilot.press("3")
                await pilot.pause()
                # Telegram is section 1; edit it, then move to Controls.
                await pilot.press("tab")
                await pilot.pause()
                chat = app.screen.query_one("#f-telegram_chat_id", Input)
                chat.value = "424242"
                await pilot.press("tab")             # away from Telegram
                await pilot.pause()
                self.assertNotEqual(app.screen._section, 1,
                                    "the test needs Telegram hidden")
                await pilot.press("ctrl+s")
                await pilot.pause()
                value = app.bot.cfg.telegram_chat_id
                await pilot.press("q")
                return value

        self.assertEqual(self._run(scenario()), "424242",
                         "a hidden section's edit was not saved")

    def test_settings_revert_restores_every_section(self):
        """Esc reverts every section, not only the visible one."""
        async def scenario():
            from textual.widgets import Input
            app = self._build()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await pilot.press("3")
                await pilot.pause()
                scr = app.screen
                # Edit a field in a section that is not on screen, then revert
                # without ever showing it.
                hidden = scr.query_one("#f-telegram_chat_id", Input)
                stored = hidden.value
                hidden.value = "999999"
                self.assertNotEqual(scr._section, 1, "Telegram must be hidden")
                scr.action_revert()
                await pilot.pause()
                after = scr.query_one("#f-telegram_chat_id", Input).value
                await pilot.press("q")
                return after, stored

        after, stored = self._run(scenario())
        self.assertEqual(after, stored,
                         "Esc did not restore a hidden section's field")

    def test_mouse_reporting_is_disabled(self):
        """The driver is built with mouse=False, so clicks, wheel and hover do
        nothing — this interface is keyboard-only."""
        app = self._build()
        driver_class = app.get_driver_class()
        self.assertEqual(getattr(driver_class, "keywords", {}).get("mouse"), False,
                         "mouse reporting is still on")

    def test_vim_keys_move_the_cursor_in_a_table(self):
        """`j`/`k` are documented as alternatives to the arrow keys."""
        async def scenario():
            app = self._build()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await pilot.press("2")            # menu -> projects
                await pilot.pause()
                from textual.widgets import DataTable
                table = app.screen.query_one(DataTable)
                table.add_row("1", "a", "b", "c")
                table.add_row("2", "d", "e", "f")
                table.focus()
                await pilot.press("j")
                await pilot.pause()
                down = table.cursor_row
                await pilot.press("k")
                await pilot.pause()
                up = table.cursor_row
                await pilot.press("q")
                return down, up

        down, up = self._run(scenario())
        self.assertGreater(down, 0, "j did not move the cursor down")
        self.assertEqual(up, 0, "k did not move the cursor back up")

    def test_q_quits(self):
        async def scenario():
            app = self._build()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await pilot.press("q")
                await pilot.pause()
                return app.is_running

        self.assertFalse(self._run(scenario()), "q did not quit the app")


class TuiFallback(unittest.TestCase):
    """The mode must be chosen before authentication runs.

    `python main.py > log.txt` used to launch Chrome anyway, because the monitor
    was built first — on a host with no display that ended in an unhandled
    "tab crashed" traceback instead of a log stream.
    """

    def test_piped_stdout_reports_tui_unavailable(self):
        from ponishabot import tui
        real = sys.stdout
        try:
            sys.stdout = io.StringIO()      # a StringIO is not a tty
            self.assertFalse(tui.can_run_tui(),
                             "a non-tty must not be offered the full-screen UI")
        finally:
            sys.stdout = real

    def test_piped_output_streams_instead_of_crashing(self):
        """With stdout piped, run_terminal must not reach the TUI."""
        source = open(os.path.join(ROOT, "ponishabot", "app.py"),
                      encoding="utf-8").read()
        body = source[source.index("def run_terminal"):]
        body = body[:body.index("def ", body.index("def run_terminal") + 10)] \
            if "def " in body[body.index("def run_terminal") + 10:] else body
        self.assertIn("can_run_tui()", body,
                      "run_terminal must decide the mode before anything else")

    def test_run_terminal_does_not_authenticate(self):
        """No sign-in from run_terminal.

        It used to call build_monitor(), which opens Chrome and blocks on
        input() from the main thread — the thread the TUI renders on. Starting
        `main.py` therefore showed a login prompt and the interface never
        appeared. Signing in belongs to the Login screen.
        """
        import ast
        source = open(os.path.join(ROOT, "ponishabot", "app.py"),
                      encoding="utf-8").read()
        tree = ast.parse(source)
        func = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "run_terminal")
        # Walk the body, not the source text: the docstring names
        # build_monitor() to explain why it is gone, and a text scan flags the
        # explanation as the defect.
        called = set()
        for node in ast.walk(func):
            if isinstance(node, ast.Call):
                f = node.func
                called.add(getattr(f, "attr", None) or getattr(f, "id", None))
        for forbidden in ("build_monitor", "ensure_auth", "manual_login"):
            self.assertNotIn(forbidden, called,
                             f"run_terminal must not call {forbidden}")

    def test_stored_cookie_is_not_treated_as_a_live_session(self):
        """`has_cookies` only says a cookie exists.

        This machine holds a pss-at cookie ponisha has already invalidated, so
        trusting that flag meant the plain path built the monitor anyway —
        which opens Chrome and blocks on input() in a mode documented as
        streaming the log. `has_valid_session` probes instead.
        """
        from ponishabot.app import BotApp
        fake_auth = mock.Mock()
        fake_auth.get_cookies.return_value = [{"name": "pss-at", "value": "x"}]
        fake_auth._probe_session.return_value = False     # stored but revoked
        fake_auth._token_cookies.return_value = []
        app = BotApp.__new__(BotApp)
        app.auth = fake_auth

        self.assertEqual(app.has_valid_session(), False,
                         "an invalidated cookie must not read as a session")
        fake_auth._probe_session.assert_called_once()

    def test_valid_cookie_reads_as_a_session(self):
        from ponishabot.app import BotApp
        fake_auth = mock.Mock()
        fake_auth.get_cookies.return_value = [{"name": "pss-at", "value": "x"}]
        fake_auth._probe_session.return_value = True
        app = BotApp.__new__(BotApp)
        app.auth = fake_auth
        self.assertTrue(app.has_valid_session())

    def test_otp_token_alone_reads_as_a_session(self):
        """An OTP login stores a bearer token, not cookies; the session can
        still be rebuilt from it without a browser."""
        from ponishabot.app import BotApp
        fake_auth = mock.Mock()
        fake_auth.get_cookies.return_value = []
        fake_auth._token_cookies.return_value = [{"name": "pss-at", "value": "y"}]
        app = BotApp.__new__(BotApp)
        app.auth = fake_auth
        self.assertTrue(app.has_valid_session())


class ConfigBanner(unittest.TestCase):
    """The load summary is a bare print() to stdout.

    Inside the TUI that lands mid-screen and pushes the rendered interface up,
    so the terminal entry sets PONISHABOT_QUIET_CONFIG before the app is built.
    """

    def setUp(self):
        import tempfile
        self._tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".yaml", delete=False, encoding="utf-8")
        self._tmp.write("bid:\n  dry_run: true\n")
        self._tmp.close()
        self._saved = os.environ.pop("PONISHABOT_QUIET_CONFIG", None)

    def tearDown(self):
        if self._saved is not None:
            os.environ["PONISHABOT_QUIET_CONFIG"] = self._saved
        else:
            os.environ.pop("PONISHABOT_QUIET_CONFIG", None)
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def _capture(self, **kwargs):
        import contextlib
        import io as _io
        from ponishabot.config import ConfigLoader
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            ConfigLoader.load(self._tmp.name, **kwargs)
        return buf.getvalue()

    def test_banner_is_printed_by_default(self):
        self.assertIn("Settings loaded", self._capture())

    def test_explicit_quiet_suppresses_it(self):
        self.assertNotIn("Settings loaded", self._capture(quiet=True))

    def test_env_var_suppresses_it(self):
        """How main.py asks for quiet without importing the config module."""
        os.environ["PONISHABOT_QUIET_CONFIG"] = "1"
        self.assertNotIn("Settings loaded", self._capture())


class ConfigDefaults(unittest.TestCase):
    """The defaults the README documents; a change here must update the docs."""

    def setUp(self):
        self.cfg = Config({})

    def test_safety_defaults(self):
        # dry_run must stay True: it is the only guard against a stray bid.
        self.assertTrue(self.cfg.dry_run)
        self.assertEqual(self.cfg.min_remaining_proposals, 2)

    def test_documented_numeric_defaults(self):
        self.assertEqual(self.cfg.refresh_interval, 300)
        self.assertEqual(self.cfg.max_pages, 24)
        self.assertEqual(self.cfg.budget_max, 10_000_000)
        self.assertEqual(self.cfg.baseline_usd_rate, 500_000)
        self.assertEqual(self.cfg.forex_sensitivity, 0.5)
        self.assertEqual(self.cfg.karlancer_clamp_multiplier, 3.0)
        self.assertEqual(self.cfg.karlancer_target_multiplier, 2.5)

    def test_ai_and_karlancer_are_off_by_default(self):
        self.assertFalse(self.cfg.ai_enabled)
        self.assertFalse(self.cfg.karlancer_enabled)

    def test_env_var_overrides_config_file(self):
        """TELEGRAM_* in the environment wins over the yaml value."""
        os.environ["TELEGRAM_BOT_TOKEN"] = "from-env"
        try:
            cfg = Config({"telegram": {"bot_token": "from-yaml", "chat_id": "c"}})
            self.assertEqual(cfg.telegram_token, "from-env")
        finally:
            del os.environ["TELEGRAM_BOT_TOKEN"]

    def test_empty_skills_are_not_a_wildcard_by_default(self):
        self.assertEqual(self.cfg.search_skills, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
