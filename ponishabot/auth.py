"""Authentication with ponisha.ir.

The user logs in once manually; the session cookies are captured and stored
in SQLite (ponisha.db). They are later reused for both scraping
(requests.Session) and bidding (Selenium).

Note: ponisha most likely uses a cookie session (not localStorage). If you
later discover it stores a localStorage token, enable a set_localstorage
method mirroring this cookie flow.
"""

import glob

from ponishabot import paths
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime
from typing import Dict, List, Optional

import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait

logger = logging.getLogger(__name__)

BASE_URL = "https://ponisha.ir"
DB_PATH = paths.DB_PATH
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# ── pss-at cookie passphrase ───────────────────────────────────────────
# ponisha encrypts its own `pss-at` cookie with a passphrase baked into the
# site's JavaScript bundle. That value is part of ponisha's client code, not
# a credential belonging to this project, but the two are not interchangeable
# and only the operator can decide which applies to their account.
#
# It is therefore NOT a constant in this file. Read it from the environment,
# or from a gitignored local file outside the package:
#
#   PONISHA_TOKEN_PASSPHRASE=...            (env var; wins)
#   ponishabot.local.yaml -> token_passphrase: ...
#
# With neither set, only the OTP login path works (see `auth()`), which is
# the supported headless route anyway.
PASSPHRASE_ENV = "PONISHA_TOKEN_PASSPHRASE"
PASSPHRASE_FILE = os.path.join(paths.ROOT_DIR, "ponishabot.local.yaml")


def _file_passphrase() -> str:
    """`token_passphrase` from the gitignored local file, or ''.

    Uses safe_load, which builds only plain Python types — `yaml.load` without
    a Loader can construct arbitrary objects from the file.
    """
    import yaml
    try:
        with open(PASSPHRASE_FILE, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return str(data.get("token_passphrase") or "").strip()
    except FileNotFoundError:
        return ""
    except Exception as e:
        # Never log the value, only the fact that the file was unusable.
        logger.warning(f"Could not read {os.path.basename(PASSPHRASE_FILE)}: "
                       f"{type(e).__name__}")
        return ""


class PonishaAuth:
    def __init__(self, headless: bool = False, user_data_dir: Optional[str] = None):
        self.driver: Optional[webdriver.Chrome] = None
        self.headless = headless
        self.user_data_dir = user_data_dir
        self.base_url = BASE_URL
        self.init_db()

    # ─── Database ───
    def init_db(self) -> None:
        with sqlite3.connect(DB_PATH, timeout=10) as conn:
            c = conn.cursor()
            c.execute('''CREATE TABLE IF NOT EXISTS session_cookies
                         (id INTEGER PRIMARY KEY CHECK (id = 1),
                          cookies_json TEXT NOT NULL,
                          saved_at TEXT NOT NULL)''')
            c.execute('''CREATE TABLE IF NOT EXISTS meta
                         (key TEXT PRIMARY KEY, value TEXT NOT NULL)''')
        logger.debug("Database ready")

    def save_cookies(self, cookies: List[Dict]) -> None:
        saved_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(DB_PATH, timeout=10) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM session_cookies")
            c.execute("INSERT INTO session_cookies VALUES (1, ?, ?)",
                      (json.dumps(cookies), saved_at))
        logger.info(f"Saved {len(cookies)} cookies")

    def get_cookies(self) -> Optional[List[Dict]]:
        with sqlite3.connect(DB_PATH, timeout=10) as conn:
            c = conn.cursor()
            c.execute("SELECT cookies_json FROM session_cookies WHERE id=1")
            row = c.fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0])
        except Exception:
            return None

    def save_user_agent(self, ua: str) -> None:
        with sqlite3.connect(DB_PATH, timeout=10) as conn:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('user_agent', ?)",
                         (ua,))

    def save_meta(self, key: str, value: str) -> None:
        with sqlite3.connect(DB_PATH, timeout=10) as conn:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                         (key, value))

    def get_meta(self, key: str) -> Optional[str]:
        with sqlite3.connect(DB_PATH, timeout=10) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def save_access_token(self, token: str) -> None:
        """Persist the raw Bearer JWT obtained from the OTP login flow.
        The `exp` claim is mirrored into meta so the expiry survives
        independently of the token being parseable later."""
        raw = (token or "").replace("Bearer ", "").strip()
        exp = self._token_expiry(raw)
        self.save_meta("access_token", raw)
        self.save_meta("access_token_expires", str(int(exp)) if exp else "")
        logger.info("Saved access token (OTP login)")

    def get_user_agent(self) -> Optional[str]:
        with sqlite3.connect(DB_PATH, timeout=10) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key='user_agent'").fetchone()
        return row[0] if row else None

    def _find_chromedriver(self) -> "Service | None":
        """Find a working ChromeDriver via multiple strategies.
        1. webdriver-manager (works offline with cache)
        2. Cached chromedriver in ~/.wdm/drivers/chromedriver/
        3. Selenium Manager (may fail on network-restricted setups)
        Returns a Service or None."""
        # Strategy 1: webdriver-manager (uses cached driver if available)
        try:
            from webdriver_manager.chrome import ChromeDriverManager
            path = ChromeDriverManager().install()
            if path and os.path.exists(path):
                logger.info(f"ChromeDriver via webdriver-manager: {path}")
                return Service(path)
        except Exception as e:
            logger.debug(f"webdriver-manager failed: {e}")

        # Strategy 2: scan the ~/.wdm cache (platform-specific binary name)
        if sys.platform.startswith("win"):
            wdm_dir = os.path.expanduser("~/.wdm/drivers/chromedriver/win64")
            binary = "chromedriver.exe"
        elif sys.platform == "darwin":
            wdm_dir = os.path.expanduser("~/.wdm/drivers/chromedriver/mac64")
            binary = "chromedriver"
        else:
            wdm_dir = os.path.expanduser("~/.wdm/drivers/chromedriver/linux64")
            binary = "chromedriver"
        candidates = sorted(glob.glob(os.path.join(wdm_dir, "**", binary),
                                      recursive=True), reverse=True)
        for path in candidates:
            if os.path.isfile(path):
                logger.info(f"ChromeDriver from cache: {path}")
                return Service(path)

        # Strategy 3: Selenium Manager (tries to download)
        logger.info("Falling back to Selenium Manager")
        return None

    def init_driver(self, force_headed: bool = False) -> webdriver.Chrome:
        """Launch Chrome using the best available ChromeDriver source.
        Tries the cached webdriver-manager driver first (works offline),
        then falls back to Selenium Manager (auto-download).

        force_headed=True relaunches a visible window even when headless
        mode is on — required for manual login, where the user must see
        and interact with the browser."""
        if self.driver is not None:
            if force_headed and self.headless:
                self.close_driver()  # relaunch as a visible window
            else:
                return self.driver

        options = Options()
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        # Headless Chrome advertises "HeadlessChrome" and a bare "X11; Linux"
        # platform, which does not match the Windows UA the session was created
        # with. ponisha may key session validity to it, so present the same
        # identity the cookies were issued to.
        options.add_argument(f"--user-agent={self.get_user_agent() or USER_AGENT}")

        if self.user_data_dir:
            user_data_dir = self.user_data_dir
            self._owns_profile = False
        else:
            user_data_dir = tempfile.mkdtemp(prefix="ponisha_")
            self._owns_profile = True
            self._profile_dir = user_data_dir  # track for cleanup
        os.makedirs(user_data_dir, exist_ok=True)
        options.add_argument(f"--user-data-dir={user_data_dir}")

        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)

        if self.headless and not force_headed:
            options.add_argument("--headless=new")
            options.add_argument("--disable-gpu")
        # Headless defaults to a 780x580 viewport. Ponisha's project page puts
        # "ارسال پیشنهاد" around y=505 with only 493px of visible height, so
        # the button sat outside the viewport and click() raised
        # ElementClickInterceptedException on every bid attempt.
        options.add_argument("--window-size=1440,1200")

        attempts = []
        service = self._find_chromedriver()
        if service:
            attempts.append(lambda: webdriver.Chrome(service=service, options=options))
        attempts.append(lambda: webdriver.Chrome(options=options))  # Selenium Manager

        last_err: Exception = RuntimeError("no launch attempt was made")
        for make in attempts:
            try:
                self.driver = make()
                break
            except Exception as e:
                last_err = e
                self.driver = None
                logger.warning(f"Chrome launch attempt failed: {e}")

        if self.driver is None:
            # Only clean up a temp profile we created — never a user-supplied
            # user_data_dir (that would wipe their real Chrome profile).
            if getattr(self, "_owns_profile", False) and getattr(self, "_profile_dir", None):
                shutil.rmtree(self._profile_dir, ignore_errors=True)
                self._profile_dir = None
            logger.error(
                "Failed to launch Chrome. Make sure Google Chrome is installed. "
                f"Original error: {last_err}"
            )
            raise last_err

        self.driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
        )
        logger.info("WebDriver initialized")
        return self.driver

    def close_driver(self) -> None:
        if self.driver is not None:
            try:
                self.driver.quit()
            except Exception as e:
                logger.warning(f"Error closing driver: {e}")
            finally:
                self.driver = None
        # Clean up the temp profile we created (never a user-supplied dir)
        if getattr(self, "_owns_profile", False) and getattr(self, "_profile_dir", None):
            shutil.rmtree(self._profile_dir, ignore_errors=True)
            self._profile_dir = None

    # ─── OTP login via REST API (no Selenium / no display needed) ───
    @staticmethod
    def normalize_mobile(raw: str) -> str:
        """Normalize a Persian/Latin mobile number to 09xxxxxxxxx, or ''."""
        if not raw:
            return ""
        persian = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
        digits = "".join(ch for ch in str(raw).translate(persian) if ch.isdigit())
        if digits.startswith("0098"):
            digits = digits[4:]
        elif digits.startswith("98") and len(digits) == 12:
            digits = digits[2:]
        if digits.startswith("9") and len(digits) == 10:
            digits = "0" + digits
        return digits if len(digits) == 11 and digits.startswith("09") else ""

    def request_otp(self, mobile: str) -> Dict:
        """Ask ponisha to SMS a login code. Returns {ok, expires_at?, error?}."""
        m = self.normalize_mobile(mobile)
        if not m:
            return {"ok": False, "error": "شماره موبایل نامعتبر است (مثال: 09121234567)"}
        try:
            resp = requests.post(
                f"{self.API_BASE}/auth/login",
                json={"mobile": m},
                headers={"Accept": "application/json", "Origin": self.base_url,
                         "Referer": f"{self.base_url}/users/login"},
                timeout=20,
            )
            if resp.status_code == 200:
                data = (resp.json() or {}).get("data") or {}
                logger.info(f"OTP requested for {m[:4]}****{m[-3:]}")
                return {"ok": True, "expires_at": data.get("otp_expired_at")}
            if resp.status_code == 429:
                return {"ok": False, "error": "تعداد درخواست زیاد — چند دقیقه صبر کنید"}
            try:
                errs = (resp.json() or {}).get("errors") or {}
                msg = next(iter(errs.values()))[0] if errs else None
            except Exception:
                msg = None
            return {"ok": False, "error": msg or f"خطای سرور ({resp.status_code})"}
        except requests.RequestException as e:
            logger.warning(f"request_otp failed: {e}")
            return {"ok": False, "error": "اتصال به پونیشا برقرار نشد"}

    def verify_otp(self, mobile: str, otp: str) -> Dict:
        """Verify the SMS code and persist the resulting access token.
        Returns {ok, error?}."""
        m = self.normalize_mobile(mobile)
        code = "".join(ch for ch in str(otp or "") if ch.isdigit())
        if not m:
            return {"ok": False, "error": "شماره موبایل نامعتبر است"}
        if len(code) < 4:
            return {"ok": False, "error": "کد تایید نامعتبر است"}
        try:
            resp = requests.post(
                f"{self.API_BASE}/auth/otp/verification",
                json={"mobile": m, "otp": code},
                headers={"Accept": "application/json", "Origin": self.base_url,
                         "Referer": f"{self.base_url}/users/login"},
                timeout=20,
            )
            if resp.status_code == 200:
                data = (resp.json() or {}).get("data") or {}
                token = (data.get("token") or data.get("accessToken")
                         or data.get("access_token") or "")
                if not token:
                    logger.warning("OTP verified but no token in response")
                    return {"ok": False, "error": "پاسخ سرور توکن نداشت"}
                self.save_access_token(token)
                logger.info(f"OTP verified — session established "
                            f"(user: {data.get('username', '?')})")
                return {"ok": True}
            if resp.status_code in (406, 422):
                return {"ok": False, "error": "کد تایید اشتباه یا منقضی شده است"}
            return {"ok": False, "error": f"خطای سرور ({resp.status_code})"}
        except requests.RequestException as e:
            logger.warning(f"verify_otp failed: {e}")
            return {"ok": False, "error": "اتصال به پونیشا برقرار نشد"}

    # ─── Login ───
    def _is_logged_in(self, driver: webdriver.Chrome) -> bool:
        """Detect a logged-in state. Ponisha may or may not redirect to a
        dashboard URL, so also look for logout/profile markers in the page."""
        try:
            url = driver.current_url.lower()
            if "dashboard" in url or "profile" in url:
                return True
            src = driver.page_source.lower()
            return "logout" in src or "/users/logout" in src
        except Exception:
            return False

    def manual_login(self, interactive: bool = True) -> List[Dict]:
        """Open the login page and wait until the user is authenticated.
        When interactive=False (GUI context) no blocking input() is shown;
        the caller is expected to log in through the opened browser.

        Non-interactive mode polls every 2s for up to 10 minutes so the GUI
        thread is never blocked and the user has plenty of time.
        Always runs headed — the user must see the browser to log in."""
        self.init_driver(force_headed=True)
        self.driver.get(f"{self.base_url}/users/login")
        if interactive:
            input("After logging in to ponisha manually, press Enter...")
        else:
            deadline = time.time() + 600
            while time.time() < deadline:
                if self._is_logged_in(self.driver):
                    break
                time.sleep(2)
            else:
                raise TimeoutError("Login was not detected within 10 minutes")
        WebDriverWait(self.driver, 180).until(
            lambda d: "dashboard" in d.current_url.lower()
            or "profile" in d.current_url.lower()
            or "logout" in d.page_source.lower()
        )
        cookies = self.driver.get_cookies()
        # Capture the browser's real User-Agent so the requests-based scraper
        # presents the same fingerprint the session cookies were issued to.
        try:
            ua = self.driver.execute_script("return navigator.userAgent")
            if ua:
                self.save_user_agent(ua)
        except Exception:
            pass
        logger.info("Manual login completed")
        return cookies

    # ─── pss-at cookie synthesis ───
    @staticmethod
    def _evp_bytes_to_key(password: bytes, salt: bytes,
                          key_len: int = 32, iv_len: int = 16):
        import hashlib
        out, prev = b"", b""
        while len(out) < key_len + iv_len:
            prev = hashlib.md5(prev + password + salt).digest()
            out += prev
        return out[:key_len], out[key_len:key_len + iv_len]

    def _build_pss_at(self, token: str) -> str:
        """Encrypt an access token into the site's `pss-at` cookie format
        (OpenSSL Salted__, AES-256-CBC, MD5 EVP key derivation).

        The payload must match what the site itself writes, or the browser
        ignores the cookie and keeps rendering the login form:
            {"accessToken": "Bearer <jwt>", "tokenExpiredAt": <ms>, "lastUpdate": <ms>}
        The prefix and the two timestamps are both required — a bare
        {"accessToken": "<jwt>"} authenticates over requests but not in Chrome.

        Raises ValueError when no passphrase is configured. Deriving a key from
        an empty string would produce a cookie the site silently rejects, which
        is the failure mode that used to abort every bid.
        """
        import base64
        from Crypto.Cipher import AES  # pycryptodome
        passphrase = self._TOKEN_PASSPHRASE
        if not passphrase:
            # ASCII only: this string reaches logs on consoles that cannot
            # encode typographic punctuation (Windows cp1252), and a crash
            # while reporting a missing key would hide the real problem.
            raise ValueError(
                f"no pss-at passphrase configured; set {PASSPHRASE_ENV} or "
                f"token_passphrase in {os.path.basename(PASSPHRASE_FILE)}")
        salt = os.urandom(8)
        key, iv = self._evp_bytes_to_key(passphrase.encode(), salt)
        exp = self._token_expiry(token)
        now_ms = int(time.time() * 1000)
        inner = {
            "accessToken": token if token.startswith("Bearer ") else f"Bearer {token}",
            "tokenExpiredAt": int(exp * 1000) if exp else now_ms + 7 * 86400_000,
            "lastUpdate": now_ms,
        }
        payload = json.dumps(inner, separators=(",", ":")).encode("utf-8")
        pad = 16 - (len(payload) % 16)
        payload += bytes([pad]) * pad
        ct = AES.new(key, AES.MODE_CBC, iv).encrypt(payload)
        return base64.b64encode(b"Salted__" + salt + ct).decode("ascii")

    def _decrypt_pss_at(self, enc: str) -> "str | None":
        """Bearer token inside a `pss-at` cookie value, or None if unreadable.

        Raises nothing: a malformed cookie is simply not a session.
        """
        import base64
        from Crypto.Cipher import AES  # pycryptodome
        raw = base64.b64decode(enc)
        if raw[:8] != b"Salted__":
            logger.warning("pss-at cookie is not in OpenSSL salted format")
            return None
        salt, ct = raw[8:16], raw[16:]
        key, iv = self._evp_bytes_to_key(self._TOKEN_PASSPHRASE.encode(), salt)
        plain = AES.new(key, AES.MODE_CBC, iv).decrypt(ct)
        if not plain:
            return None
        plain = plain[:-plain[-1]]  # strip PKCS7 padding
        token = json.loads(plain.decode("utf-8")).get("accessToken", "")
        return token.replace("Bearer ", "") or None

    def _token_cookies(self) -> List[Dict]:
        """Build the cookie set the site expects from an OTP-only login.
        The OTP flow stores a bearer token but no browser cookies, so a
        headless VPS had no way to start without a display."""
        token = self.get_access_token()
        if not token:
            return []
        try:
            # Mirror the attributes a real browser login produces, including
            # expiry: the site sets this cookie with an explicit lifetime, and
            # Selenium treats a cookie without one as a session cookie.
            exp = self._token_expiry(token)
            if exp:
                exp -= 60  # let it lapse slightly before the token does
            return [{
                "name": self._PSS_AT,
                "value": self._build_pss_at(token),
                "domain": ".ponisha.ir",
                "path": "/",
                "expiry": int(exp) if exp else int(time.time()) + 7 * 86400,
                "sameSite": "Lax",
                "secure": False,
                "httpOnly": False,
            }]
        except Exception as e:
            logger.warning(f"Could not build pss-at cookie from token: {e}")
            return []

    def auth(self, interactive: bool = True) -> bool:
        cookies = self.get_cookies()
        if cookies and self._probe_session(cookies):
            logger.info("Cookies loaded from database")
            return True
        if cookies:
            logger.warning("Stored cookies appear invalid/expired — re-login required")

        # An OTP login stores a bearer token rather than browser cookies, so
        # ask the API about that token directly. Turning it into a `pss-at`
        # cookie needs the passphrase, and without one the old code fell
        # straight through to `manual_login()` — which opens Chrome and waits
        # up to ten minutes for a sign-in nobody needed, while a perfectly
        # live token sat in the database.
        token = self.get_access_token()
        if token and self._token_is_live(token):
            derived = self._token_cookies()
            if derived:
                logger.info("Session rebuilt from stored OTP token (no browser needed)")
                self.save_cookies(derived)
            else:
                # Honest about the limit: the REST API (quota) and the public
                # listing pages work, the browser bid form does not, because
                # the site reads its session from the cookie.
                logger.warning(
                    "OTP token is live, but no pss-at passphrase is configured, "
                    "so it cannot be turned into a browser session — monitoring "
                    "works, bidding in the browser will not")
            return True

        logger.info("Manual login required")
        cookies = self.manual_login(interactive=interactive)
        self.save_cookies(cookies)
        return True

    def _token_is_live(self, token: str) -> bool:
        """Is this bearer token still accepted by ponisha's API?

        An explicit 401 is the only answer that means "dead". Failures that are
        not the API's verdict (network flake, WAF, a 5xx) must not force a
        re-login — that would open a browser on every offline start.
        """
        try:
            resp = requests.get(
                f"{self.API_BASE}/users/me",
                headers={"Authorization": f"Bearer {token}",
                         "Accept": "application/json",
                         "User-Agent": self.get_user_agent() or USER_AGENT},
                timeout=15)
        except requests.RequestException:
            return True  # network flake — do not force re-login
        if resp.status_code == 200:
            return True
        if resp.status_code == 401:
            logger.warning("Stored OTP token rejected by the API (401) — "
                           "re-login required")
            return False
        return True  # unexpected status (5xx, WAF) — do not force re-login

    def has_live_token(self) -> bool:
        """Whether a stored OTP token still authenticates with the API."""
        token = self.get_access_token()
        return bool(token) and self._token_is_live(token)

    def _probe_session(self, cookies: List[Dict]) -> bool:
        """Is the stored cookie still an accepted session?

        Page status is not a usable signal here: /search/projects is public and
        /dashboard renders 200 even with a pss-at the server has invalidated,
        so both reported dead sessions as healthy and the bot started with a
        cookie ponisha had already revoked. The REST API is the honest check —
        it answers 401 for a revoked token while the HTML pages still render.

        Failures (network) are treated as valid so offline start still works.
        """
        enc = next((c.get("value") for c in cookies
                    if c.get("name") == self._PSS_AT), None)
        if not enc:
            return False  # nothing to prove a session with
        try:
            token = self._decrypt_pss_at(enc)
        except Exception as e:
            # Say which of the two causes this is. Reporting "re-login required"
            # for a missing passphrase sends the operator round a loop: signing
            # in again produces the same undecryptable cookie, because the
            # cookie is encrypted with ponisha's passphrase and reading it back
            # needs that same value here.
            if not self._TOKEN_PASSPHRASE:
                logger.warning(
                    f"Stored pss-at cookie cannot be read and no passphrase is "
                    f"configured to decrypt it ({type(e).__name__}) — set "
                    f"{PASSPHRASE_ENV} or token_passphrase in "
                    f"{os.path.basename(PASSPHRASE_FILE)}; signing in again "
                    f"will not change this")
            else:
                logger.warning(f"Stored pss-at cookie is unreadable ({e}) — "
                               f"re-login required")
            return False
        if not token:
            return False  # undecryptable cookie cannot be a live session
        try:
            resp = requests.get(
                f"{self.API_BASE}/users/me",
                headers={"Authorization": f"Bearer {token}",
                         "Accept": "application/json",
                         "User-Agent": self.get_user_agent() or USER_AGENT},
                timeout=15)
            if resp.status_code == 200:
                return True
            if resp.status_code == 401:
                logger.warning("Stored session rejected by the API (401) — re-login required")
                return False
            return True  # unexpected status (5xx, WAF) — do not force re-login
        except requests.RequestException:
            return True  # network flake — do not force re-login

    # ─── Reuse for scraping layer ───
    def build_session(self) -> requests.Session:
        """A cookie-loaded requests.Session for listing scraping. The UA is
        the real browser's (captured at login) so the fingerprint matches
        the cookies; falls back to the pinned constant."""
        session = requests.Session()
        session.headers.update({
            "User-Agent": self.get_user_agent() or USER_AGENT,
            "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        cookies = self.get_cookies() or []
        for c in cookies:
            session.cookies.set(c.get("name", ""), c.get("value", ""),
                                domain=c.get("domain"), path=c.get("path", "/"))
        return session

    # ─── Reuse for bidding layer (Selenium) ───
    def inject_cookies_into_driver(self) -> None:
        """Inject stored cookies into the current driver (domain must be set)."""
        self.init_driver()
        self.driver.get(self.base_url)
        cookies = self.get_cookies() or []
        for c in cookies:
            try:
                self.driver.add_cookie(c)
            except Exception as e:
                # Warn, not debug: a cookie that fails here leaves the browser
                # signed out and every bid then opens the login form instead.
                logger.warning(f"Cookie {c.get('name')!r} not set in browser: {e}")
        self.driver.refresh()
        time.sleep(2)

    # ─── Ponisha REST API (api.ponisha.ir) ───
    # The login cookie "pss-at" is CryptoJS-AES(OpenSSL Salted__) encrypted; the
    # inner accessToken is a JWT the REST API accepts as a Bearer token. The
    # passphrase is supplied by the operator — see the module docstring above.
    _PSS_AT = "pss-at"
    API_BASE = "https://api.ponisha.ir/api/v1"

    @property
    def _TOKEN_PASSPHRASE(self) -> str:
        """Passphrase for the pss-at cookie, from env or the local file.

        Each source wins only when it actually holds something: systemd passes
        `PONISHA_TOKEN_PASSPHRASE=` (empty) from deploy/bot.env, and os.getenv
        returns "" for a variable that exists but is blank. Using that blank
        value encrypted the cookie with an empty key — the site then rejected
        it and the browser sat on the login form, so every bid aborted.

        Returns "" when nothing is configured. Callers must treat that as
        "cookie path unavailable" rather than encrypting with no key.
        """
        return ((os.getenv(PASSPHRASE_ENV) or "").strip() or
                _file_passphrase())

    @staticmethod
    def _token_expiry(token: str) -> "float | None":
        """Unix expiry from the JWT's `exp` claim, or None if unreadable."""
        try:
            import base64
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
            return float(exp) if exp else None
        except Exception:
            return None

    @classmethod
    def _token_is_expired(cls, token: str) -> bool:
        """True when the JWT's own `exp` claim is in the past."""
        exp = cls._token_expiry(token)
        return exp is not None and exp <= time.time()

    def get_access_token(self) -> "str | None":
        """Raw JWT access token. Prefers one stored by the OTP login flow,
        then falls back to decrypting the stored pss-at cookie. An expired
        stored token is skipped so a still-valid cookie can take over."""
        saved = self.get_meta("access_token")
        if saved and not self._token_is_expired(saved):
            return saved
        if saved:
            logger.info("Stored OTP token has expired — falling back to pss-at cookie")
        cookies = self.get_cookies() or []
        enc = next((c.get("value") for c in cookies
                    if c.get("name") == self._PSS_AT), None)
        if not enc:
            return None
        try:
            return self._decrypt_pss_at(enc)
        except Exception as e:
            logger.warning(f"Could not decrypt access token: {e}")
            return None

    def build_api_session(self) -> "requests.Session | None":
        """A requests.Session authorized for api.ponisha.ir; None if no token."""
        token = self.get_access_token()
        if not token:
            return None
        session = requests.Session()
        session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": self.get_user_agent() or USER_AGENT,
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/",
        })
        return session
