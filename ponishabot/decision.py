"""Pluggable decision layer.

HeuristicBidDecision — skill overlap + budget window, no network.
LLMBidDecision — asks an OpenAI-compatible chat-completions endpoint (9router,
OpenRouter, OpenAI, local servers) to decide should_bid / price / duration
based on the user's profile and market competition. Falls back to the
heuristic automatically on any failure so the bot never stalls.

The engine is selected in app.py (config-driven) — the rest of the pipeline
is unchanged because everyone agrees on the BidPlan type.
"""

from typing import Optional

import json
import logging
import re
import sys
from abc import ABC, abstractmethod

import requests

from ponishabot.models import BidPlan, Profile, Project, MarketComparison
from ponishabot.forex import ForexProvider

logger = logging.getLogger(__name__)


def _safe(msg: str) -> str:
    """Make a string safe to log on consoles with limited encodings (e.g.
    Windows cp1252/latin-1) by replacing chars that cannot be encoded."""
    enc = sys.stdout.encoding or "utf-8"
    try:
        msg.encode(enc, "strict")
        return msg
    except (UnicodeEncodeError, LookupError):
        # Round-tripping through UTF-8 was a no-op; map the offending chars to
        # '?' in the *console's* encoding instead, keeping the readable parts.
        try:
            return msg.encode(enc, "replace").decode(enc, "replace")
        except (UnicodeEncodeError, LookupError, UnicodeDecodeError):
            return msg.encode("ascii", "replace").decode("ascii")


def _log_info(msg: str) -> None:
    logger.info(_safe(msg))


def _log_warning(msg: str) -> None:
    logger.warning(_safe(msg))


def _normalize(skill: str) -> str:
    """Lower-cased skill name. ponisha's skill list can contain nulls, and a
    bare .strip() on one raised AttributeError deep inside decide()."""
    return (skill or "").strip().lower()


SYSTEM_PROMPT = """You are a bid advisor for a freelance marketplace (ponisha.ir, budgets in Iranian Toman).
Given one project and one freelancer profile, decide whether the freelancer should
bid on this project, and if so propose a price and a duration.

Guidelines:
- The freelancer's "instructions" field contains personal rules and preferences.
  Treat these as binding directives (e.g. "never bid below X", "skip translation
  projects", "charge 20% extra for urgent"). Apply them before making any decision.
- Weigh skill fit first: match the project's required skills against the
  freelancer's skills and described experience. Do not suggest unrelated work.
- If the project lists NO skills at all (an empty "skills" field), judge fit
  from the description alone. Employers frequently skip the skill tags, so an
  empty list is not evidence of a mismatch — read what the project actually
  needs and decide whether the freelancer can deliver it. Apply the same
  reject rules as always; just do not reject merely for missing tags.
- "bids" is the market-competition signal: how many proposals others already
  submitted. More bids = more competition; with heavy competition either skip
  the project or price aggressively (lower, within the budget range).
- "badges" indicate project urgency/quality: "فوری" (urgent) = premium acceptable;
  "متمایز"/"برجسته" (premium listing) = higher budget likely; "فوری" + high bids
  = skip or bid very aggressively.
- "duration_days" must be a realistic whole number of days (>= 1).
  Compute duration from effort: duration_days = ceil(estimated_hours / 6)
  (about 6 productive hours per working day). estimated_hours from complexity
  or (price ÷ avg hourly rate). Never default to a fixed "14 days".
  Urgent (فوری / <3 days): compress only if the work still fits.
- Reply in JSON only. No markdown fences, no commentary.
- "message" field: THIS IS THE PROPOSAL TEXT that will be pasted into ponisha.ir's bid form.
  Write in Persian, 2-3 sentences: 1) what you'll do (address the project's specific needs),
  2) why you're qualified (your relevant skills/experience from the profile), 3) your proposed
  timeline and confidence level. Max 500 characters. Professional and persuasive.
  Always in Persian. This is what the employer reads — make it count.

PRICING STRATEGY (market-first — most important):
- Price comes from the MARKET first, never from the employer's budget.
  Order of sources:
  1. Karlancer / similar-project market data (when provided)
  2. Freelancer hourly rate × estimated hours (delivery effort)
  3. Reference market rates for this project type (below)
  4. Employer budget = weak signal only (negotiation context, NOT the price)
- Do NOT start from employer budget. Do NOT clamp your price to it.
- PONISHA ENFORCES A FLOOR: the site rejects any proposal below the employer's
  stated minimum with "حداقل بودجه برای این پروژه X تومان است". Never quote a
  price below `budget_min` — the bid cannot be submitted at all. If your
  market-based price lands under the floor, raise it to the floor. There is no
  upper limit: quoting above `budget_max` is allowed and common.
- Employer budget is NOT a ceiling unless the listing explicitly states a
  fixed/final cap (سقف قطعی / نهایی / غیرقابل تغییر). If your computed
  price exceeds `budget_max`, still bid YOUR price and hint at scope complexity
  in the Persian text. Prefer skipping a project whose `budget_max` is more
  than 40% below your floor when it is neither urgent nor open to negotiation.
- "price" in JSON is your OPENING IDEA (پیشنهادی), not a final locked quote.
  In the Persian proposal, phrase it as an opening suggestion and invite
  negotiation (e.g. «بودجه پیشنهادی من … تا … میلیون تومان»).
- Never bid below the freelancer's minimum from the profile (safety floor).
- If market data is missing: use hours × rate + reference rates, not budget mid-point.
- STABILITY BAND — this is a SANITY CHECK, not a fixed price for a whole category:
  for genuinely mid-complexity work (an Odoo module, a small Django API, a plain
  scraper) the opening price usually lands in **5,000,000–7,000,000 Toman**.
  Do NOT treat that range as the price of every project you label "bot" or
  "automation". REAL COMPLEXITY SETS THE PRICE, not the category name: a project
  can share a category with a 3M job and still be a 10M job. Score the actual
  scope first (below), then check the result against this band — and if the scope
  is clearly harder than "mid", the band does not apply at all.
  Round price to the nearest 100,000 Toman.
- COMPLEXITY LADDER — walk it before choosing a number:
  * Read-only scraping, no login, no writes, public pages → bottom of the
    category range (for bots/automation: 3–5M).
  * Authenticated scraping or a small internal tool → low-to-mid range (5–7M).
  * ANY of the following pushes the price to the TOP of the range (7–10M for
    bots/automation, higher for other categories), not the floor:
      – logging into a real user's account and acting on their behalf
        (e.g. "اتصال به حساب دیوار کارفرما", posting/publishing under their identity)
      – defeating or surviving an anti-bot / captcha / rate-limit system
      – full form-filling automation that CREATES or MODIFIES data on a third-party
        site (publishing, re-posting, editing listings) rather than only reading it
      – multi-step flows across sessions, token/session management, retry logic
      – anything the listing itself flags as technically uncertain
        ("جزئیات نحوه اتصال … لازم است در پیشنهاد توضیح داده شود")
  Each such factor is a separate, real cost. Two or more of them → price at the
  ceiling of the range and say why in the proposal.
- PRICE MUST MATCH YOUR OWN PROPOSAL TEXT: if the Persian text you write names a
  hard technical challenge (authentication, anti-bot, account takeover, session
  handling, captcha), the price must reflect that difficulty. Never describe a
  serious challenge and then quote the floor — the employer reads both and the
  mismatch reads as either inexperience or a bait price. If your text says the
  job is hard, your number says so too.
- Always round price to nearest 100,000 and duration_days to a whole number >= 1.

Reference market rates (Iranian freelance market, 2025-2026, approximate):
- Simple WordPress/static site: 3-8M Toman
- Django/Python backend (small): 5-12M Toman
- Django/Python backend (medium): 12-25M Toman
- Full-stack web app: 15-40M Toman
- E-commerce site: 10-30M Toman
- Mobile app (React Native/Flutter): 15-40M Toman
- Bot/automation (read-only scraping, no login): 3-5M Toman
- Bot/automation (authenticated, writes/publishes under a user's account,
  anti-bot handling): 7-12M Toman
- UI/UX design: 5-15M Toman
- Logo/brand identity: 2-8M Toman
- Video editing: 1-5M Toman
- Odoo custom module: 5-12M Toman
These are ranges for the CATEGORY, not quotes — pick a point inside using the
complexity ladder above, and treat the top of a range as the normal landing
spot for work that touches real accounts, anti-bot systems, or publishing flows.

Competitive Pricing Strategy (new account = more competitive):
- COMPLEXITY OUTRANKS COMPETITION. Set the price from the complexity ladder
  above FIRST, then apply the competition/urgency adjustments to THAT number.
  Many competing bids is a reason to trim a margin, never a reason to quote the
  floor on genuinely hard work — losing a badly-priced job is better than
  winning it. Never let a competition discount pull a hard project below the
  bottom of its complexity band.
- If instructions say the account is new / building reputation:
  bid 10–20% BELOW the market midpoint (still at or above the safety floor).
- High competition (bid_count > 15): trim up to 5% from your complexity-based
  price, or skip if the result would fall under your minimum. Do not go to the
  floor just because the listing is crowded.
- "فوری" badge (urgent): add 20% premium unless employer budget is a clear hard ceiling —
  then price at floor+(ceiling-floor)*0.4 as opening idea.
- "متمایز" / "برجسته": mid-to-high of market range (not employer budget).
- Low competition (bid_count < 5) & no urgency: market midpoint or slightly above.

Market Comparison from karlancer.com:
- Use avg budget as BASELINE for your range, then apply the competitive bias above.
- If ponisha employer budget is lower than karlancer avg: still use market-based
  opening idea (may exceed employer budget; invite negotiation).
- If ponisha budget is higher: price within market range, not inflated to budget.

Output shape (exactly):
{"should_bid": true|false, "price": <integer Toman or null>, "duration_days": <integer days or null>, "payment_steps": [{"title": "step name", "percent": N}, ...], "message": "<proposal text in Persian, max 500 chars>"}
When should_bid is false, set price and duration_days to null, payment_steps to [].

Payment milestones guidance:
- Use a single step [{"title": "پرداخت کامل", "percent": 100}] for small projects (under 3M Toman).
- For larger projects, use 2-3 milestones (e.g. 50% upfront + 50% on delivery, or 30% start + 40% mid-review + 30% delivery). Persian titles only.
- All percents must sum to exactly 100."""


class BidDecision(ABC):
    @abstractmethod
    def decide(self, project: Project, profile: Profile) -> BidPlan:
        """Return a complete BidPlan. The caller (monitor) just executes it."""
        ...


class HeuristicBidDecision(BidDecision):
    """Current implementation: skill overlap + budget window + competitive pricing. No LLM."""

    def __init__(self, min_skill_match: float = 0.5,
                 high_competition_threshold: int = 15,
                 urgency_premium_percent: int = 20,
                 premium_badge_multiplier: float = 1.15,
                 low_competition_discount_percent: int = 10,
                 high_competition_discount_percent: int = 15):
        self.min_skill_match = min_skill_match
        self.high_competition_threshold = high_competition_threshold
        self.urgency_premium_percent = urgency_premium_percent
        self.premium_badge_multiplier = premium_badge_multiplier
        self.low_competition_discount_percent = low_competition_discount_percent
        self.high_competition_discount_percent = high_competition_discount_percent

    def _calculate_price(self, project: Project, profile: Profile, overlap: float) -> Optional[int]:
        """Calculate price with competitive pricing adjustments.

        Returns None only when the employer budget is unknown AND no fallback
        applies — the caller (`decide`) then estimates from the profile guide.
        """
        lo = project.budget_min or 0
        hi = project.budget_max or 0
        if not (lo and hi):
            return None

        # Base price: midpoint of budget range
        base_price = (lo + hi) // 2

        # Clamp to profile budget window (soft cap — never forces below floor)
        if profile.max_budget:
            base_price = min(base_price, profile.max_budget)
        if profile.min_budget:
            base_price = max(base_price, profile.min_budget)

        # Apply competitive adjustments
        bid_count = project.bid_count or 0
        badges = project.badges or []

        # High competition: discount
        if bid_count >= self.high_competition_threshold:
            discount = self.high_competition_discount_percent / 100.0
            base_price = int(base_price * (1 - discount))
        # Low competition: premium
        elif bid_count < 5:
            premium = self.low_competition_discount_percent / 100.0
            base_price = int(base_price * (1 + premium))

        # Urgency badge premium
        if "فوری" in badges:
            premium = self.urgency_premium_percent / 100.0
            base_price = int(base_price * (1 + premium))

        # Premium badge multiplier
        if any(b in badges for b in ["متمایز", "برجسته", "حرفه‌ای"]):
            base_price = int(base_price * self.premium_badge_multiplier)

        # Ensure within budget range
        if project.budget_min:
            base_price = max(base_price, project.budget_min)
        if project.budget_max:
            base_price = min(base_price, project.budget_max)

        return base_price

    @staticmethod
    def _guide_default_price(profile: Profile) -> "int | None":
        """Estimate a default price from the freelancer's pricing guide in
        `profile.about` (e.g. 'ربات: ۳-۶M | متوسط: ۵-۸M'). Averages the
        midpoints of any 'X-YM' ranges found. Falls back to the midpoint of
        the profile budget window, then None."""
        about = profile.about or ""
        norm = about.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
        midpoints = []
        # `M`, `\u0645\u06cc\u0644\u06cc\u0648\u0646` or `\u0645` may follow the numbers. Only `M` used to, so a
        # guide written the natural Persian way matched nothing and the
        # estimate silently fell through to the budget midpoint \u2014 a real
        # profile with five ranges produced zero matches.
        scale = r"(?:M|\u0645\u06cc\u0644\u06cc\u0648\u0646|\u0645\b)"
        for m in re.finditer(r"(\d+)\s*[-\u2013]\s*(\d+)\s*" + scale, norm,
                             re.IGNORECASE):
            try:
                lo, hi = int(m.group(1)), int(m.group(2))
            except ValueError:
                continue
            midpoints.append((lo + hi) / 2 * 1_000_000)
        if midpoints:
            return int(sum(midpoints) / len(midpoints))
        lo = profile.min_budget or 0
        hi = profile.max_budget or 0
        if lo or hi:
            return int((max(lo, 2_000_000) + max(hi, 2_000_000)) / 2)
        return None

    def decide(self, project: Project, profile: Profile) -> BidPlan:
        proj_skills = {_normalize(s) for s in project.skills}
        my_skills = {_normalize(s) for s in profile.skills}

        # No skill tags means the overlap is unknown, not zero — an untagged
        # listing should fall through to the budget check rather than being
        # rejected outright. See LLMBidDecision.decide for the same rule.
        if proj_skills:
            overlap = len(proj_skills & my_skills) / len(proj_skills)
            if overlap < self.min_skill_match:
                return BidPlan(should_bid=False,
                               message=f"skill overlap {overlap:.0%} < "
                                       f"{self.min_skill_match:.0%}")
        else:
            # Untagged listing: price it as a normal-fit project rather than
            # dividing by zero.
            overlap = 1.0

        lo = project.budget_min or 0
        hi = project.budget_max or 0
        # Only reject absurdly high budgets; 15M+ projects are still biddable.
        if lo and lo > 50_000_000:
            return BidPlan(should_bid=False, message="budget far above ceiling")

        price = self._calculate_price(project, profile, overlap)
        # Unknown employer budget -> estimate from the profile's pricing guide.
        if price is None:
            price = self._guide_default_price(profile)
        # Never bid with no price; the profile's minimum acceptable bid (rule #1).
        floor = max(profile.min_budget or 0, 2_000_000)
        if price is None:
            return BidPlan(should_bid=False,
                           message="could not determine a price (no budget, no guide)")
        price = max(int(price), floor)
        # ponisha rejects any proposal under the employer's stated minimum, so
        # the profile floor alone is not enough — raise to the listing's floor.
        if project.budget_min:
            price = max(price, int(project.budget_min))

        duration_days = None
        if project.suggested_days:
            duration_days = max(1, project.suggested_days // 2)

        overlap_msg = f"matched, overlap={overlap:.0%}"
        return BidPlan(should_bid=True, price=price, duration_days=duration_days,
                       message=overlap_msg)


class LLMBidDecision(BidDecision):
    """AI decision engine via any OpenAI-compatible chat-completions endpoint
    (9router, OpenRouter, OpenAI, local servers). Falls back to
    HeuristicBidDecision on any failure so the monitor thread never stalls."""

    def __init__(self, api_key: str, base_url: str = "", model: str = "",
                 min_skill_gate: float = 0.2, temperature: float = 0.2,
                 max_description_chars: int = 4000, timeout: int = 30,
                 fallback_min_skill_match: float = 0.5,
                 karlancer_scraper: "KarlancerScraper | None" = None,
                 karlancer_enabled: bool = False,
                 karlancer_max_pages: int = 2,
                 forex_provider: "ForexProvider | None" = None,
                 forex_enabled: bool = True,
                 baseline_usd_rate: int = 500000,
                 forex_sensitivity: float = 0.5,
                 karlancer_soft_clamp_enabled: bool = True,
                 karlancer_clamp_multiplier: float = 3.0,
                 karlancer_target_multiplier: float = 2.5):
        self.api_key = api_key
        self.base_url = (base_url or "").rstrip("/")
        self.model = model
        self.min_skill_gate = min_skill_gate
        self.temperature = temperature
        self.max_description_chars = max_description_chars
        self.timeout = timeout
        self._fallback = HeuristicBidDecision(min_skill_match=fallback_min_skill_match)
        self._karlancer = karlancer_scraper
        self._karlancer_enabled = karlancer_enabled
        self._karlancer_max_pages = karlancer_max_pages
        self._forex = forex_provider
        self._forex_enabled = forex_enabled
        self._baseline_usd_rate = baseline_usd_rate
        self._forex_sensitivity = forex_sensitivity
        self._karlancer_soft_clamp_enabled = karlancer_soft_clamp_enabled
        self._karlancer_clamp_multiplier = karlancer_clamp_multiplier
        self._karlancer_target_multiplier = karlancer_target_multiplier

    def _skill_overlap(self, project: Project, profile: Profile) -> float:
        """Fraction of the project's skills the freelancer also lists.

        Returns 0.0 when the project has no skills — callers must treat that
        as "unknown", not as "no match". Employers often skip the skill tags
        entirely, and scoring those as a total mismatch rejected perfectly
        relevant work: project 760174 (a booking site with online payment,
        budget 20-40M) has an empty skill list and was auto-rejected before
        the model ever saw its description.
        """
        proj = {_normalize(s) for s in project.skills}
        my = {_normalize(s) for s in profile.skills}
        return len(proj & my) / len(proj) if proj else 0.0

    @staticmethod
    def _raise_to_listing_floor(plan: BidPlan, project: Project) -> BidPlan:
        """ponisha refuses any proposal below the employer's stated minimum —
        "حداقل بودجه برای این پروژه X تومان است" — so a market-based price that
        lands under it cannot be submitted at all. There is no upper limit.

        The prompt says this too, but the model still undershot 14.1M against a
        20M floor, so the rule is enforced here as well.
        """
        floor = project.budget_min
        if not plan.should_bid or not plan.price or not floor:
            return plan
        if plan.price >= floor:
            return plan
        _log_info(f"Price {plan.price:,} is under the listing floor "
                  f"{floor:,} — raising to the floor")
        plan.price = int(floor)
        return plan

    def _apply_forex_and_clamp(self, plan: BidPlan,
                                market: "MarketComparison | None" = None) -> BidPlan:
        """Apply forex uplift and optional Karlancer soft-clamp to the AI price.

        Returns a new BidPlan with the adjusted price (and updated message).
        """
        if not plan.should_bid or plan.price is None:
            return plan

        adjusted_price = plan.price

        # 1) Forex uplift
        if self._forex_enabled and self._forex:
            rate = self._forex.get_usd_toman()
            if rate:
                factor = ForexProvider.uplift_factor(rate, self._baseline_usd_rate,
                                                    self._forex_sensitivity)
                if factor != 1.0:
                    old = adjusted_price
                    adjusted_price = int(adjusted_price * factor)
                    logger.info(f"Forex uplift: rate={rate:,} baseline={self._baseline_usd_rate:,} "
                                f"factor={factor:.2%} => {old:,} -> {adjusted_price:,}")

        # 2) Karlancer soft-clamp (only if market data available)
        if (self._karlancer_soft_clamp_enabled and market and market.avg_budget
                and market.avg_budget > 0):
            clamp_ceiling = int(market.avg_budget * self._karlancer_clamp_multiplier)
            target_price = int(market.avg_budget * self._karlancer_target_multiplier)

            if adjusted_price > clamp_ceiling:
                old = adjusted_price
                adjusted_price = target_price
                logger.info(f"Karlancer soft-clamp: avg={market.avg_budget:,} "
                            f"clamp@={self._karlancer_clamp_multiplier}x -> "
                            f"{old:,} -> {adjusted_price:,}")

        # Final form price must stay a clean multiple of 100k (after forex/clamp)
        rounded = int(round(adjusted_price / 100_000.0) * 100_000)
        if rounded < 100_000:
            rounded = 100_000
        if rounded != adjusted_price:
            logger.info(f"Price rounded to 100k: {adjusted_price:,} -> {rounded:,}")
            adjusted_price = rounded

        if adjusted_price != plan.price:
            new_plan = BidPlan(
                should_bid=plan.should_bid,
                price=adjusted_price,
                duration_days=plan.duration_days,
                message=f"{plan.message} [forex/clamp adj: {plan.price:,}->{adjusted_price:,}]",
                proposal=plan.proposal,
                payment_steps=plan.payment_steps,
            )
            return new_plan
        return plan

    def decide(self, project: Project, profile: Profile) -> BidPlan:
        # Skip the gate entirely when the listing carries no skills: overlap is
        # then undefined, and the description still holds everything needed to
        # judge fit. Letting the model read it costs one call; auto-rejecting
        # throws away well-paid, on-topic work.
        if project.skills:
            overlap = self._skill_overlap(project, profile)
            if overlap < self.min_skill_gate:
                return BidPlan(should_bid=False,
                               message=f"AI gate: skill overlap {overlap:.0%} < "
                                       f"{self.min_skill_gate:.0%}")
        else:
            logger.info("No skill tags on this project — judging from the "
                        "description")

        # Fetch market comparison data before calling AI
        market = None
        if self._karlancer_enabled and self._karlancer:
            search_skill = (project.skills[0] if project.skills else
                            (profile.skills[0] if profile.skills else ""))
            if search_skill:
                try:
                    market = self._karlancer.fetch_similar_projects(
                        search_skill, self._karlancer_max_pages)
                except Exception as e:
                    logger.warning(f"Karlancer market fetch failed: {e}")

        try:
            plan = self._ask_llm(project, profile, market)
            if plan is not None:
                # Apply forex uplift + Karlancer soft-clamp
                plan = self._apply_forex_and_clamp(plan, market)
                plan = self._raise_to_listing_floor(plan, project)
                if plan.should_bid:
                    _log_info(f"AI: bid {plan.price:,} Toman / "
                              f"{plan.duration_days} days — {plan.message}")
                else:
                    _log_info(f"AI: no bid — {plan.message}")
                return plan
            logger.warning("AI returned unusable JSON — heuristic fallback")
        except Exception as e:
            _log_warning(f"AI decision failed ({type(e).__name__}) — heuristic fallback")
        plan = self._fallback.decide(project, profile)
        plan.message = f"(AI fallback) {plan.message}"
        return plan

    def _ask_llm(self, project: Project, profile: Profile,
                 market: "MarketComparison | None" = None):
        content = self._chat([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": self._build_user_message(project, profile, market)},
        ])
        data = _parse_llm_json(content)
        if data is None:
            return None
        return _validate_plan(data, project)

    def _build_user_message(self, project: Project, profile: Profile,
                            market: "MarketComparison | None" = None) -> str:
        lo = project.budget_min if project.budget_min is not None else "?"
        hi = project.budget_max if project.budget_max is not None else "?"
        desc = (project.description or "—")[: self.max_description_chars]
        hourly = ""
        est_hours = ""
        if profile.hourly_min and profile.hourly_max:
            hourly = f"\n- hourly rate (Toman): {profile.hourly_min:,}–{profile.hourly_max:,}"
            # Rough hour estimate from budget/price and hourly rate
            # We'll add an estimated hours hint for the AI
            avg_rate = (profile.hourly_min + profile.hourly_max) // 2
            if profile.min_budget and profile.max_budget:
                est_min = profile.min_budget // max(avg_rate, 1)
                est_max = profile.max_budget // max(avg_rate, 1)
                est_hours = f"\n- estimated hours (at your rate): {est_min}–{est_max} hours"
            elif project.budget_min and project.budget_max:
                est_min = project.budget_min // max(avg_rate, 1)
                est_max = project.budget_max // max(avg_rate, 1)
                est_hours = f"\n- estimated hours (at your rate): {est_min}–{est_max} hours"
        if est_hours:
            hourly += est_hours
        else:
            # Always give the model an hours hook for duration even without budget window
            avg_rate = (profile.hourly_min + profile.hourly_max) // 2 if (
                profile.hourly_min and profile.hourly_max) else 0
            if avg_rate and project.budget_min and project.budget_max:
                est_hours = (f"\n- estimated hours (at your rate, from budget): "
                             f"{project.budget_min // avg_rate}–{project.budget_max // avg_rate} hours")
                hourly += est_hours
            elif avg_rate:
                hourly += "\n- estimated hours: estimate from complexity / price÷rate; duration_days = ceil(hours/6)"
        bid_count = project.bid_count if project.bid_count is not None else "unknown"
        badges = ", ".join(project.badges) if project.badges else "none"
        skill_count = project.skill_count if project.skill_count else 0
        budget_hint = "unknown"
        if project.budget_from_text:
            lo_hint, hi_hint = project.budget_from_text
            if lo_hint is not None:
                budget_hint = f"{lo_hint:,}–{hi_hint:,} Toman" if hi_hint is not None \
                    else f"{lo_hint:,} Toman"

        # Market comparison from karlancer.com
        market_section = ""
        if market and market.num_results > 0:
            sample_budgets = [p["budget"] for p in market.similar_projects[:5]
                              if p.get("budget")]
            samples = ", ".join(f"{b:,}" for b in sample_budgets) if sample_budgets else "—"
            market_section = (
                f"\nMarket comparison (karlancer.com):\n"
                f"- similar projects found: {market.num_results}\n"
                f"- avg budget: {market.avg_budget:,} Toman\n"
                f"- budget range: {market.min_budget:,}–{market.max_budget:,} Toman\n"
                f"- sample prices: {samples} Toman\n"
            )

        return (
            f"Project:\n"
            f"- title: {project.title}\n"
            f"- skills: {', '.join(project.skills) or '—'}\n"
            f"- budget (Toman): {lo}–{hi}\n"
            f"- budget hint (from description): {budget_hint}\n"
            f"- bids (competition): {bid_count}\n"
            f"- badges: {badges}\n"
            f"- skill count: {skill_count}\n"
            f"- suggested duration (days): {project.suggested_days or '—'}\n"
            f"- description: \"\"\"{desc}\"\"\"\n\n"
            f"Freelancer profile:\n"
            f"- name: {profile.name or '—'}\n"
            f"- skills: {', '.join(profile.skills) or '—'}\n"
            f"- budget window (Toman): {profile.min_budget:,}–{profile.max_budget:,}"
            f"{hourly}\n"
            f"- instructions / rules: {profile.about or '—'}\n"
            f"{market_section}\n"
            f"Decide now. Answer with the JSON object only."
        )

    def _chat(self, messages: list, timeout: int = None) -> str:
        """POST /chat/completions with retries for timeouts, connection, 429/5xx.

        ReadTimeout is NOT a ConnectionError subclass in requests — it must be
        caught explicitly or every timeout jumps straight to heuristic fallback.
        """
        import time

        url = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}
        req_timeout = timeout or self.timeout
        max_total_retries = 3
        base_delay = 1.5  # seconds
        last_err = None

        for total_attempt in range(max_total_retries):
            retry_break = False
            for use_json_format in (True, False):
                body = {
                    "model": self.model,
                    "messages": messages,
                    "temperature": self.temperature,
                    "max_tokens": 3000,
                }
                if use_json_format:
                    body["response_format"] = {"type": "json_object"}
                try:
                    resp = requests.post(url, headers=headers, json=body,
                                         timeout=req_timeout)
                except (requests.Timeout, requests.ConnectionError,
                        requests.RequestException) as e:
                    # Timeout (Read/Connect), DNS, reset, etc.
                    last_err = e
                    wait = base_delay * (2 ** total_attempt)
                    if total_attempt < max_total_retries - 1:
                        logger.warning(
                            f"AI request failed ({type(e).__name__}), "
                            f"retrying in {wait:.1f}s "
                            f"(attempt {total_attempt + 1}/{max_total_retries})")
                        time.sleep(wait)
                    retry_break = True
                    break  # next outer attempt

                if resp.status_code == 400 and use_json_format:
                    continue  # try without json_object format

                if resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        wait = min(int(retry_after), 60)
                    else:
                        wait = base_delay * (2 ** total_attempt)
                    logger.warning(
                        f"AI provider rate limited (429), waiting {wait}s "
                        f"(attempt {total_attempt + 1}/{max_total_retries})")
                    time.sleep(wait)
                    retry_break = True
                    break

                if resp.status_code in (502, 503, 504, 500):
                    wait = base_delay * (2 ** total_attempt)
                    logger.warning(
                        f"AI provider {resp.status_code}, waiting {wait}s "
                        f"(attempt {total_attempt + 1}/{max_total_retries})")
                    time.sleep(wait)
                    retry_break = True
                    break

                try:
                    resp.raise_for_status()
                    result = self._extract_content(resp.text)
                except ValueError as e:
                    # unparseable body — retry whole attempt
                    last_err = e
                    if total_attempt < max_total_retries - 1:
                        wait = base_delay * (2 ** total_attempt)
                        logger.warning(
                            f"AI response unparseable ({e}), retrying in "
                            f"{wait:.1f}s (attempt {total_attempt + 1}/{max_total_retries})")
                        time.sleep(wait)
                    retry_break = True
                    break
                if result:
                    return result
                last_err = ValueError("empty content after extract")
            if retry_break:
                continue
        raise last_err or ValueError("No content returned by provider after retries")

    @staticmethod
    def _extract_content(raw: str) -> str:
        """Parse a chat completion response — handles normal JSON, SSE streaming,
        and hybrid formats (9router combo sends JSON followed by "data: [DONE]"
        on the same blob). Also strips DeepSeek/QwQ <think>...</think> blocks."""
        text = raw.strip()

        # Strip trailing "data: [DONE]" glued to the JSON (9router combo quirk)
        import re
        text = re.sub(r'\s*data:\s*\[DONE\].*$', '', text, flags=re.DOTALL).strip()

        # try normal JSON first
        if text.startswith("{"):
            try:
                obj = json.loads(text)
                content = obj["choices"][0]["message"]["content"]
                # strip DeepSeek/QwQ <think> blocks
                content = re.sub(r'<think>.*?</think>\s*', '', content, flags=re.DOTALL)
                return content.strip()
            except (json.JSONDecodeError, KeyError, IndexError):
                pass

        # SSE streaming: accumulate delta.content from "data: {...}" lines
        parts = []
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data: "):
                continue
            payload = line[6:]
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
                # try delta (streaming) then message (non-streaming)
                choice = obj.get("choices", [{}])[0]
                delta = choice.get("delta", {})
                content = delta.get("content", "")
                if not content:
                    msg = choice.get("message", {})
                    content = msg.get("content", "")
                if content:
                    parts.append(content)
            except (json.JSONDecodeError, KeyError, IndexError):
                continue
        if parts:
            result = "".join(parts)
            result = re.sub(r'<think>.*?</think>\s*', '', result, flags=re.DOTALL)
            return result.strip()

        raise ValueError(f"Could not extract content from response: {raw[:300]}")

    def test_connection(self):
        """Cheap probe. Returns (ok: bool, detail: str). Never raises."""
        try:
            reply = self._chat(
                [{"role": "user", "content": "Reply with exactly: OK"}], timeout=30)
            ok = "ok" in reply.strip().lower()
            return ok, (reply.strip()[:80] if ok
                        else f"unexpected reply: {reply.strip()[:80]!r}")
        except Exception as e:
            return False, f"{type(e).__name__}: {str(e)[:80]}"


def _fix_persian(text: str) -> str:
    """Fix double-encoded Persian/Arabic text (the provider returns UTF-8
    bytes that were decoded as latin-1, so each Persian char arrives as two).

    Returns the corrected string when double-encoding is detected, else the
    original unchanged. Callers often truncate first, which can cut a
    multi-byte sequence in half; that used to make the decode raise and the
    mojibake survive, so a trailing partial character is dropped instead.
    """
    if not text:
        return text
    try:
        raw = text.encode("latin-1")
    except UnicodeEncodeError:
        return text  # not latin-1 at all — genuinely single-encoded
    for trim in range(0, 4):  # a UTF-8 char is at most 4 bytes
        try:
            fixed = (raw if trim == 0 else raw[:-trim]).decode("utf-8")
        except UnicodeDecodeError:
            continue
        # each mojibake char maps to one, so success always shortens the string
        return fixed if len(fixed) < len(text) else text
    return text


def _parse_llm_json(content: str):
    """Extract the first {...} block and json.loads it. Tolerates markdown
    fences and any prose around the JSON. Returns dict or None."""
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _validate_plan(data: dict, project: Project):
    """Defensively coerce the LLM JSON into a BidPlan.
    Returns a BidPlan, or None when the response is too broken to trust
    (caller then uses the heuristic fallback)."""
    raw = data.get("should_bid")
    if isinstance(raw, str):
        should_bid = raw.strip().lower() in ("true", "1", "yes")
    else:
        should_bid = bool(raw)
    # Repair the encoding BEFORE truncating: slicing mojibake can cut a byte
    # pair in half, which makes the repair fail and the garbled text survive.
    message = _fix_persian(str(data.get("message", "")).strip())[:200]

    if not should_bid:
        return BidPlan(should_bid=False, message=message or "AI: no bid")

    price = data.get("price")
    if isinstance(price, bool) or not isinstance(price, (int, float)):
        return None  # a bid without a usable price = garbage
    price = int(price)
    # Don't clamp to employer's budget — employer budgets are often unrealistic.
    # Only ensure price is positive and not absurdly high (> 100M Toman).
    if price <= 0:
        return None
    if price > 100_000_000:
        price = 100_000_000  # sanity cap
    # Round to nearest 100k for stable, readable openings
    price = int(round(price / 100_000.0) * 100_000)
    if price < 100_000:
        price = 100_000

    days = data.get("duration_days")
    days = int(days) if isinstance(days, (int, float)) and not isinstance(days, bool) else None
    if days is not None:
        days = max(1, int(round(days)))

    # payment_steps: [{"title": str, "percent": int}] — percents must sum to 100
    raw_steps = data.get("payment_steps")
    payment_steps = []
    if isinstance(raw_steps, list) and raw_steps:
        for s in raw_steps:
            if not isinstance(s, dict):
                continue
            title = _fix_persian(str(s.get("title", "")).strip())
            pct = s.get("percent")
            if isinstance(pct, bool) or not isinstance(pct, (int, float)):
                continue
            pct = max(1, min(100, int(pct)))
            if title:
                payment_steps.append({"title": title, "percent": pct})
        # Every step needs >= 1 percent, so more than 100 steps can never sum
        # to 100 — truncate rather than emit a step the bid form would reject.
        if len(payment_steps) > 100:
            payment_steps = payment_steps[:100]
        if payment_steps:
            total = sum(s["percent"] for s in payment_steps)
            for s in payment_steps:
                s["percent"] = max(1, round(s["percent"] * 100 / total))
            # Spread the rounding remainder across steps that still have room.
            # Piling it onto the last step could drive that step to 0 or
            # negative, and the bid form would then submit a negative amount.
            diff = 100 - sum(s["percent"] for s in payment_steps)
            for s in sorted(payment_steps, key=lambda x: -x["percent"]):
                if diff == 0:
                    break
                if diff > 0:
                    take = min(100 - s["percent"], diff)
                else:
                    take = -min(s["percent"] - 1, -diff)
                s["percent"] += take
                diff -= take
            if diff != 0:  # only reachable if every step is pinned at 1
                payment_steps = []

    # proposal: full text for the bid form (max 500 chars, fix Persian encoding)
    # fall back to message if proposal is missing (combo models may not send it).
    # Repair before truncating — see the note on `message` above. This one is
    # submitted to ponisha verbatim, so garbled text here reaches the employer.
    raw_proposal = data.get("proposal", "")
    proposal = _fix_persian(
        str(raw_proposal or data.get("message", "")).strip())[:500]

    return BidPlan(should_bid=True, price=price, duration_days=days,
                   message=message, proposal=proposal,
                   payment_steps=payment_steps)
