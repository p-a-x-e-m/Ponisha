"""Test suite for ponishabot.

Run with:  python -m unittest discover -s tests -v

Everything here is offline and deterministic: no network, no Selenium, no
ponisha session. The functions under test are the pure ones — the pricing
arithmetic, the scraper's text helpers, the config loader's defaults — which
is where the expensive mistakes lived (a wrong budget read, a price under the
employer's floor, an untagged listing rejected as a total mismatch).
"""

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
        from ponishabot import panel
        self.panel = panel

    # A real excerpt: the run that motivated the filter had this line nine
    # times in a row, burying everything else.
    NOISY = [
        "2026-10-09 16:32:09,379 - WARNING - Forex fetch failed: HTTPSConnectionPool(host='api.wallex.ir')",
    ] * 9 + [
        "2026-10-09 16:32:10,000 - INFO - Karlancer market: 48 projects, avg=5806666",
        "2026-10-09 16:32:11,000 - ERROR - Error in monitor cycle: boom",
    ]

    def test_classify_splits_level_time_message(self):
        lvl, ts, msg = self.panel.classify_line(
            "2026-10-09 16:32:10,000 - INFO - hello")
        self.assertEqual(lvl, "INFO")
        self.assertEqual(ts, "16:32:10")
        self.assertEqual(msg, "hello")

    def test_classify_survives_a_non_log_line(self):
        """Tracebacks and banners have no level; they must not crash the panel."""
        lvl, ts, msg = self.panel.classify_line("  File \"x.py\", line 3")
        self.assertEqual(lvl, "info")
        self.assertEqual(ts, "")
        self.assertIn("line 3", msg)

    def test_noise_is_recognised(self):
        self.assertTrue(self.panel.is_noise("Forex fetch failed: timeout"))
        self.assertFalse(self.panel.is_noise("Karlancer market: 48 projects"))

    def test_noise_hidden_by_default_and_shown_on_request(self):
        default = self.panel.filter_lines(self.NOISY, level="info")
        self.assertFalse(any("Forex fetch failed" in l for l in default))
        shown = self.panel.filter_lines(self.NOISY, level="info", show_noise=True)
        self.assertTrue(any("Forex fetch failed" in l for l in shown))

    def test_important_keeps_errors_and_drops_plain_info(self):
        lines = self.NOISY + [
            "2026-10-09 16:32:12,000 - INFO - Decision engine: AI (respone) with heuristic fallback",
        ]
        kept = self.panel.filter_lines(lines, level="important")
        self.assertTrue(any("ERROR" in l for l in kept))
        self.assertFalse(any("Decision engine" in l for l in kept))

    def test_info_keeps_info_lines(self):
        kept = self.panel.filter_lines(self.NOISY, level="info")
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
        kept = self.panel.filter_lines(lines, level="important")
        self.assertEqual(len(kept), 3, "event lines must survive the default view")

    def test_important_still_drops_uninteresting_info(self):
        lines = [
            "2026-10-09 16:32:10,000 - INFO - Decision engine: AI (respone) with heuristic fallback",
            "2026-10-09 16:32:10,000 - INFO - BotApp singleton created (web GUI)",
        ]
        self.assertEqual(self.panel.filter_lines(lines, level="important"), [])

    def test_event_classification(self):
        self.assertTrue(self.panel.is_event("AI: bid 5,000,000 Toman"))
        self.assertTrue(self.panel.is_event("Monitor stopped"))
        self.assertFalse(self.panel.is_event("Decision engine: AI (respone)"))

    def test_repeats_collapse_into_one_row_with_a_count(self):
        """The whole point: nine identical failures become one row."""
        collapsed = self.panel.collapse_repeats(self.NOISY)
        noise_rows = [c for c in collapsed if "Forex fetch failed" in c[0]]
        self.assertEqual(len(noise_rows), 1)
        self.assertEqual(noise_rows[0][1], 9)

    def test_distinct_lines_are_not_merged(self):
        collapsed = self.panel.collapse_repeats([
            "2026-10-09 16:32:10,000 - INFO - a",
            "2026-10-09 16:32:11,000 - INFO - b",
        ])
        self.assertEqual([c[1] for c in collapsed], [1, 1])

    def test_repeated_message_with_different_timestamps_still_collapses(self):
        """Comparing raw lines would never match: the timestamps differ. The
        run being counted is exactly this shape."""
        collapsed = self.panel.collapse_repeats([
            "2026-10-09 16:32:09,379 - WARNING - same failure",
            "2026-10-09 16:32:14,474 - WARNING - same failure",
        ])
        self.assertEqual(len(collapsed), 1)
        self.assertEqual(collapsed[0][1], 2)


class PanelFormatting(unittest.TestCase):
    def setUp(self):
        from ponishabot import panel
        self.panel = panel

    def test_money_groups_digits(self):
        self.assertEqual(self.panel.fmt_money(7_700_000), "7٬700٬000")

    def test_money_handles_unknown(self):
        for v in (None, "", "abc"):
            self.assertEqual(self.panel.fmt_money(v), "—", v)

    def test_duration_scales(self):
        self.assertEqual(self.panel.fmt_duration(8), "8s")
        self.assertEqual(self.panel.fmt_duration(760), "12m 40s")
        self.assertEqual(self.panel.fmt_duration(11_220), "3h 07m")

    def test_duration_handles_none_and_negative(self):
        self.assertEqual(self.panel.fmt_duration(None), "—")
        self.assertEqual(self.panel.fmt_duration(-5), "—")

    def test_layout_switches_on_width(self):
        self.assertEqual(self.panel.layout_for_width(120), "wide")
        self.assertEqual(self.panel.layout_for_width(60), "narrow")

    def test_strip_ansi_removes_escapes(self):
        self.assertEqual(self.panel.strip_ansi("\x1b[31mred\x1b[0m"), "red")

    def _render(self, buf, width=112):
        """Build the panel offscreen and return it as text."""
        import io
        from rich.console import Console
        panel_obj = self.panel.TerminalPanel.__new__(self.panel.TerminalPanel)
        panel_obj.level = "debug"
        panel_obj.show_noise = True
        panel_obj._recent = []
        panel_obj._log_buf = buf
        panel_obj._notice = ""
        panel_obj._notice_until = 0.0
        c = Console(width=width, force_terminal=False, no_color=True,
                    file=io.StringIO())
        renderable, _keys = panel_obj._build(
            {"running": False, "quota": 0, "matches": 0, "bids": 0,
             "dry_run": True, "ai_enabled": False, "has_cookies": False,
             "refresh_interval": 60, "started_at": None}, width)
        c.print(renderable)
        return c.file.getvalue()

    def test_brackets_in_log_do_not_break_markup(self):
        """Log messages carry brackets (URLs, driver names). Building the row
        with markup would make rich parse them as tags and raise."""
        out = self._render([
            "2026-10-09 16:32:10,000 - INFO - no [win64] chromedriver for [google-chrome]",
            "2026-10-09 16:32:11,000 - INFO - see https://example.com/a[b]c",
        ])
        self.assertIn("win64", out)

    def test_a_very_long_line_renders_without_wrapping_out(self):
        out = self._render([
            "2026-10-09 16:32:10,000 - ERROR - " + "x" * 400,
        ], width=100)
        self.assertIn("x", out)


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
        from ponishabot import botstate, config
        self.botstate = botstate
        self._saved_app = botstate._app
        botstate._app = None                      # force a fresh build
        self._tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".yaml", delete=False, encoding="utf-8")
        self._tmp.write("bid:\n  dry_run: true\nsearch:\n  skills: []\n")
        self._tmp.close()
        # Patching `paths.CONFIG_PATH` would not work: `ConfigLoader.load` takes
        # `path: str = paths.CONFIG_PATH` as a default argument, and Python
        # evaluates defaults once at def time. The bound value has to be
        # replaced on the loader that captured it.
        self._patch = mock.patch.object(
            config.ConfigLoader.load, "__defaults__", (self._tmp.name,))
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
