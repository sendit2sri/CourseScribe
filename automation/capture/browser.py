"""Persistent Playwright browser session management.

Uses chromium.launch_persistent_context() so the user logs in once
and cookies/session are reused across runs.

Login strategy (priority order):
  1. Persistent browser profile — reuses saved session/cookies
  2. Auto-login with credentials from .env — when session expired
  3. Manual login pause — when auto-login fails or MFA/CAPTCHA appears
"""

import asyncio
import logging
import random
from typing import Optional

from playwright.async_api import (
    BrowserContext,
    Frame,
    Page,
    Playwright,
    async_playwright,
)

from automation.config import AutomationConfig

logger = logging.getLogger(__name__)

# Common selectors for login form elements (tried in order)
_USERNAME_SELECTORS = [
    "input[name='UserName']",
    "input[id='userid']",
    "input[name='username']",
    "input[name='email']",
    "input[name='login']",
    "input[name='user']",
    "input[name='userId']",
    "input[type='email']",
    "input[id='username']",
    "input[id='email']",
    "input[id='login']",
]

_PASSWORD_SELECTORS = [
    "input[name='password']",
    "input[name='passwd']",
    "input[type='password']",
    "input[id='password']",
]

_SUBMIT_SELECTORS = [
    "button[type='submit']",
    "input[type='submit']",
    "button:has-text('Log in')",
    "button:has-text('Login')",
    "button:has-text('Sign in')",
    "button:has-text('Sign In')",
    "button:has-text('Submit')",
]

# URL substrings that indicate a login/SSO/authentication page
_LOGIN_URL_PATTERNS = (
    "/login", "/signin", "/sign-in", "/sso", "/saml",
    "/auth", "/oauth", "/idp", "/adfs",
    "login.microsoftonline.com",
    "accounts.google.com",
)


def looks_like_login_url(url: str) -> bool:
    """Cheap URL-only check for whether a page is a login/SSO redirect.

    Used by callers (CLI multi-course loop, portal helpers) to fail fast
    when the session has been bounced to login mid-run, without paying
    the cost of a full is_session_valid() navigation+probe.
    """
    if not url:
        return False
    u = url.lower()
    return any(p in u for p in _LOGIN_URL_PATTERNS)

# Generic indicators that the user is logged in
_LOGGED_IN_INDICATORS = (
    ".user-menu", ".profile-icon", ".logged-in",
    ".user-avatar", ".account-menu",
)

# Platform-specific indicators that the portal is ready (post-login)
_PORTAL_READY_INDICATORS = (
    "#boxTitle4",
    "[id*='boxTitle']",
    ".catalog-home",
    "[id*='pathway']",
)


_STEALTH_JS = """
// Hide navigator.webdriver
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

// Fake non-empty plugins array (headless browsers report 0 plugins)
Object.defineProperty(navigator, 'plugins', {
    get: () => {
        const arr = [1, 2, 3, 4, 5];
        arr.item = (i) => arr[i];
        arr.namedItem = () => null;
        arr.refresh = () => {};
        return arr;
    }
});

// Ensure languages are populated
Object.defineProperty(navigator, 'languages', {
    get: () => ['en-US', 'en']
});

// Add Chrome runtime object (missing in automation)
if (!window.chrome) {
    window.chrome = {};
}
if (!window.chrome.runtime) {
    window.chrome.runtime = {};
}

// Fix Notification permission query
if (navigator.permissions && navigator.permissions.query) {
    const origQuery = navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query = (params) =>
        params.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : origQuery(params);
}

// Remove CDP artifacts (cdc_ prefixed variables)
for (const prop of Object.getOwnPropertyNames(window)) {
    if (prop.startsWith('cdc_') || prop.startsWith('$cdc_')) {
        delete window[prop];
    }
}
"""


class BrowserSession:
    """Manages a persistent Playwright Chromium browser context."""

    def __init__(self, config: AutomationConfig):
        self.config = config
        self._playwright: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._portal_page: Optional[Page] = None
        self.pathways_landing_url: Optional[str] = None

    async def start(self) -> "BrowserSession":
        """Launch a persistent browser context. Reuses existing profile."""
        self.config.browser_data_dir.mkdir(parents=True, exist_ok=True)

        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.config.browser_data_dir),
            headless=self.config.headless,
            viewport={
                "width": self.config.viewport_width,
                "height": self.config.viewport_height,
            },
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-popup-blocking",
                "--disable-infobars",
            ],
            ignore_default_args=["--enable-automation"],
        )

        # Inject stealth scripts before any page JS runs
        await self._context.add_init_script(script=_STEALTH_JS)

        # Abort Google Fonts CDN requests so document.fonts.ready resolves
        # quickly — otherwise page.screenshot() can time out waiting for fonts
        # that never load. display=swap ensures text still renders with
        # system fallback fonts, and portal icon-fonts are served locally.
        async def _abort_font_cdn(route):
            await route.abort()

        await self._context.route("**/fonts.googleapis.com/**", _abort_font_cdn)
        await self._context.route("**/fonts.gstatic.com/**", _abort_font_cdn)

        # Use the first page or create one
        if self._context.pages:
            self._page = self._context.pages[0]
        else:
            self._page = await self._context.new_page()

        logger.info(
            "Browser started (headless=%s, profile=%s)",
            self.config.headless,
            self.config.browser_data_dir,
        )
        return self

    # ------------------------------------------------------------------
    # Session validation
    # ------------------------------------------------------------------

    async def is_session_valid(self) -> bool:
        """Check if the current browser session is still authenticated.

        Uses multiple signals to avoid false positives:
          1. Navigate and wait for page to stabilise (SSO redirects, JS)
          2. Check final URL for login-related patterns
          3. Check for username OR password fields (covers multi-step login)
          4. Check for positive logged-in / portal-ready indicators

        Decision rule (conservative):
          - Any login evidence → False
          - Positive authenticated evidence → True
          - Ambiguous / no evidence → False
        """
        if not self._page:
            return False

        url = self.config.start_url or self.config.login_url
        if not url:
            logger.debug("No URL to validate session against")
            return True  # can't check — assume valid

        # Navigate with domcontentloaded, then try networkidle for redirects
        try:
            await self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            logger.warning("Session validation navigation error: %s", e)
            return False

        # Try networkidle to let SSO redirects complete (graceful — don't fail if it times out)
        try:
            await self._page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            logger.debug("networkidle timeout during session check — continuing with signal detection")

        # Extra settle time for late-rendering JS login widgets
        await asyncio.sleep(3)

        # Collect all auth signals from the current page state
        signals = await self._collect_auth_signals()
        has_login_evidence = (
            signals["login_url_pattern"]
            or signals["username_field_visible"]
            or signals["password_field_visible"]
        )
        has_auth_evidence = (
            signals["logged_in_indicator"]
            or signals["portal_ready_indicator"]
        )

        if has_login_evidence:
            logger.info(
                "Session expired — login evidence detected (url_pattern=%s, username=%s, password=%s)",
                signals["login_url_pattern"],
                signals["username_field_visible"],
                signals["password_field_visible"],
            )
            return False

        if has_auth_evidence:
            logger.info(
                "Session valid — authenticated evidence found (logged_in=%s, portal_ready=%s)",
                signals["logged_in_indicator"],
                signals["portal_ready_indicator"],
            )
            return True

        # Ambiguous: no login signals, but no positive confirmation either
        logger.info(
            "Session status ambiguous — no login form or authenticated indicators found. "
            "Assuming expired (conservative). URL: %s",
            signals["url"],
        )
        return False

    # ------------------------------------------------------------------
    # Login flows
    # ------------------------------------------------------------------

    async def login_flow(self) -> None:
        """Smart login flow.

        Strategy:
          1. Navigate to login URL
          2. If credentials in .env -> attempt auto-login
          3. If auto-login fails or MFA/CAPTCHA appears -> pause for manual completion
          4. Session cookies are automatically persisted in the browser profile
        """
        if not self._page:
            await self.start()

        login_url = self.config.effective_login_url
        if login_url:
            logger.info("Navigating to login page...")
            await self._page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(1)
        else:
            logger.info("No login URL configured. Browser opened for manual navigation.")

        # Attempt auto-login if credentials are available
        if self.config.has_credentials:
            auto_success = await self._try_auto_login()
            if auto_success:
                return
            # Auto-login didn't fully succeed — fall through to manual
            logger.info("Auto-login incomplete — manual intervention may be needed")

        # Manual login fallback
        await self._manual_login_prompt()

    async def _try_auto_login(self) -> bool:
        """Attempt to fill and submit the login form using .env credentials.

        Returns True if login appears successful (no password field visible
        after submission). Returns False if:
          - Login form not found
          - MFA/CAPTCHA page appears after submit
          - Still on login page after submit
        """
        username = self.config.login_username
        password = self.config.login_password
        masked = self.config.masked_username()
        logger.info("Attempting auto-login as %s...", masked)

        # Wait for login form to render (JS-heavy pages like CSOD need extra time)
        username_el = None
        for attempt in range(5):
            username_el = await self._find_first_visible(self._page, _USERNAME_SELECTORS)
            if username_el:
                break
            await asyncio.sleep(1)

        if not username_el:
            logger.warning("Username field not found — cannot auto-login")
            return False

        # Find password field
        password_el = await self._find_first_visible(self._page, _PASSWORD_SELECTORS)
        if not password_el:
            logger.warning("Password field not found — cannot auto-login")
            return False

        # Fill credentials with human-like typing
        await self._human_click(username_el)
        await self._human_type(username_el, username)
        await self._human_click(password_el)
        await self._human_type(password_el, password)

        # Find and click submit
        submit_el = await self._find_first_visible(self._page, _SUBMIT_SELECTORS)
        if submit_el:
            await self._human_click(submit_el)
        else:
            # Fallback: press Enter on the password field
            await password_el.press("Enter")

        # Wait for navigation after submit
        try:
            await self._page.wait_for_load_state("domcontentloaded", timeout=15000)
            await asyncio.sleep(2)  # settle for redirects
        except Exception:
            pass

        # Check if login succeeded: no password field visible means success
        pw_still_visible = await self._find_first_visible(self._page, _PASSWORD_SELECTORS)
        if pw_still_visible:
            # Still on login page — could be wrong creds, MFA, or CAPTCHA
            logger.info("Still on login page after auto-submit — may need manual input")
            return False

        current_url = self._page.url
        logger.info("Auto-login successful. Current URL: %s", current_url)
        print(f"\nAuto-login successful as {masked}")
        print(f"Session saved to: {self.config.browser_data_dir}")
        print(f"Current URL: {current_url}")
        return True

    async def _manual_login_prompt(self) -> None:
        """Pause and wait for user to complete login manually.

        Used when:
          - No credentials in .env
          - Auto-login failed (wrong creds, MFA, CAPTCHA, etc.)
        """
        print("\n" + "=" * 60)
        print("MANUAL LOGIN REQUIRED")
        print("=" * 60)
        if self.config.has_credentials:
            print("Auto-login could not complete (MFA, CAPTCHA, or other challenge).")
            print("Please complete the login in the browser window.")
        else:
            print("No credentials found in .env file.")
            print("1. Log in to the course platform in the browser window")
        print("2. Navigate to the course you want to capture")
        print("3. Press ENTER here when you are logged in and ready")
        print("=" * 60)

        # Wait for user confirmation (run in executor to not block async loop)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, input, "Press ENTER when login is complete... ")

        current_url = self._page.url
        logger.info("Manual login complete. Current URL: %s", current_url)
        print(f"\nSession saved to: {self.config.browser_data_dir}")
        print(f"Current URL: {current_url}")
        print("You can now run capture/run commands without logging in again.")

    # ------------------------------------------------------------------
    # Ensure authenticated (used by capture/run commands)
    # ------------------------------------------------------------------

    async def ensure_authenticated(self) -> None:
        """Verify session is valid; re-login if not.

        Called at the start of capture/run commands to handle expired sessions
        transparently. Includes post-verification to catch cases where login
        appeared to succeed but the page is still a login form.
        """
        if await self.is_session_valid():
            logger.info("Existing session is valid")
            return

        logger.info("Session expired — attempting re-login")
        # Navigate to login page
        login_url = self.config.effective_login_url
        if login_url:
            await self._page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(1)

        # Try auto-login first, then manual fallback
        if self.config.has_credentials:
            if await self._try_auto_login():
                # Post-check: verify we actually left the login page
                if not await self._is_still_on_login_page():
                    return
                logger.warning(
                    "Auto-login reported success but still on login page — "
                    "falling through to manual login"
                )

        await self._manual_login_prompt()

        # Final post-check after manual login
        if await self._is_still_on_login_page():
            logger.error(
                "Still on login page after manual login. Current URL: %s",
                self._page.url,
            )
            raise RuntimeError(
                "Authentication failed — still on login page after manual login. "
                "Please check credentials and try again."
            )

    async def _is_still_on_login_page(self) -> bool:
        """Quick check: are we still on a login page?

        Lightweight — does NOT navigate, just inspects the current page.
        Reuses _collect_auth_signals() for consistent detection.
        """
        if not self._page:
            return True

        signals = await self._collect_auth_signals()
        still_on_login = (
            signals["login_url_pattern"]
            or signals["username_field_visible"]
            or signals["password_field_visible"]
        )
        if still_on_login:
            logger.debug(
                "Post-login check: still on login page (url_pattern=%s, username=%s, password=%s)",
                signals["login_url_pattern"],
                signals["username_field_visible"],
                signals["password_field_visible"],
            )
        return still_on_login

    # ------------------------------------------------------------------
    # Helper: find first visible element from a list of selectors
    # ------------------------------------------------------------------

    @staticmethod
    async def _find_first_visible(page: Page, selectors: list) -> Optional[object]:
        """Try each selector in order, return first visible element or None."""
        for sel in selectors:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    return el
            except Exception:
                continue
        return None

    # ------------------------------------------------------------------
    # Human-like interaction helpers
    # ------------------------------------------------------------------

    async def _human_type(self, element: object, text: str) -> None:
        """Type text character-by-character with randomized delays."""
        await element.fill("")  # clear existing value
        for i, char in enumerate(text):
            await element.type(char, delay=0)
            # Base delay: 50-150ms per keystroke
            delay = random.uniform(0.05, 0.15)
            # Occasional longer pause every 3-7 characters (simulates thinking)
            if i > 0 and i % random.randint(3, 7) == 0:
                delay = random.uniform(0.2, 0.4)
            await asyncio.sleep(delay)

    async def _human_click(self, element: object) -> None:
        """Click an element with slight random offset and pre-click delay."""
        try:
            box = await element.bounding_box()
            if box:
                x = box["x"] + box["width"] / 2 + random.uniform(-5, 5)
                y = box["y"] + box["height"] / 2 + random.uniform(-3, 3)
                await asyncio.sleep(random.uniform(0.05, 0.2))
                await self._page.mouse.click(x, y)
                return
        except Exception:
            pass
        # Fallback to regular click
        await element.click()

    async def random_scroll(self) -> None:
        """Simulate a human glancing at the page before capture."""
        if not self._page:
            return
        scroll_amount = random.randint(100, 400)
        await self._page.evaluate(f"window.scrollBy(0, {scroll_amount})")
        await asyncio.sleep(random.uniform(0.2, 0.5))
        await self._page.evaluate(f"window.scrollBy(0, -{scroll_amount})")
        await asyncio.sleep(random.uniform(0.1, 0.3))

    async def check_stealth(self) -> dict:
        """Run stealth diagnostics and log results. Returns dict of test results."""
        if not self._page:
            return {}
        results = await self._page.evaluate("""() => ({
            webdriver: navigator.webdriver,
            plugins_length: navigator.plugins.length,
            languages: navigator.languages,
            chrome_exists: !!window.chrome,
            chrome_runtime_exists: !!(window.chrome && window.chrome.runtime),
            has_cdc: Object.getOwnPropertyNames(window).some(p => p.startsWith('cdc_')),
        })""")
        passed = (
            results.get("webdriver") is None
            and results.get("plugins_length", 0) > 0
            and results.get("chrome_exists") is True
            and not results.get("has_cdc", True)
        )
        status = "PASS" if passed else "FAIL"
        logger.info("Stealth check [%s]: %s", status, results)
        return results

    # ------------------------------------------------------------------
    # Centralized auth signal collection
    # ------------------------------------------------------------------

    async def _collect_auth_signals(self) -> dict:
        """Collect all authentication-related signals from the current page.

        Does NOT navigate — inspects current page state only.
        Returns a dict of booleans for each signal type.
        """
        signals = {
            "url": self._page.url if self._page else "",
            "login_url_pattern": False,
            "username_field_visible": False,
            "password_field_visible": False,
            "logged_in_indicator": False,
            "portal_ready_indicator": False,
        }
        if not self._page:
            return signals

        # Check URL for login patterns
        current_url = (self._page.url or "").lower()
        for pattern in _LOGIN_URL_PATTERNS:
            if pattern.lower() in current_url:
                signals["login_url_pattern"] = True
                logger.debug("Auth signal: URL matched login pattern '%s'", pattern)
                break

        # Check for visible username fields (multi-step login detection)
        for sel in _USERNAME_SELECTORS:
            try:
                el = await self._page.query_selector(sel)
                if el and await el.is_visible():
                    signals["username_field_visible"] = True
                    logger.debug("Auth signal: username field visible (%s)", sel)
                    break
            except Exception:
                continue

        # Check for visible password fields
        for sel in _PASSWORD_SELECTORS:
            try:
                el = await self._page.query_selector(sel)
                if el and await el.is_visible():
                    signals["password_field_visible"] = True
                    logger.debug("Auth signal: password field visible (%s)", sel)
                    break
            except Exception:
                continue

        # Check for generic logged-in indicators
        for sel in _LOGGED_IN_INDICATORS:
            try:
                el = await self._page.query_selector(sel)
                if el and await el.is_visible():
                    signals["logged_in_indicator"] = True
                    logger.debug("Auth signal: logged-in indicator found (%s)", sel)
                    break
            except Exception:
                continue

        # Check for portal-ready indicators (platform-specific)
        for sel in _PORTAL_READY_INDICATORS:
            try:
                el = await self._page.query_selector(sel)
                if el and await el.is_visible():
                    signals["portal_ready_indicator"] = True
                    logger.debug("Auth signal: portal-ready indicator found (%s)", sel)
                    break
            except Exception:
                continue

        return signals

    # ------------------------------------------------------------------
    # Multi-tab management
    # ------------------------------------------------------------------

    def save_as_portal_page(self) -> None:
        """Mark the current page as the portal page (for returning after course tabs)."""
        self._portal_page = self._page
        logger.debug("Portal page saved: %s", self._page.url if self._page else "(none)")

    async def click_and_wait_for_new_tab(
        self, click_action, timeout_ms: int = 30000
    ) -> Page:
        """Execute a click action that opens a new tab and switch to it.

        Args:
            click_action: An async callable that triggers a new tab (e.g., a Locator.click).
                          Must be awaitable — passed as a coroutine function or lambda.
            timeout_ms: Max time to wait for the new tab to open.

        Returns:
            The new Page object (now set as the active page).
        """
        if not self._context:
            raise RuntimeError("Browser not started. Call start() first.")

        async with self._context.expect_page(timeout=timeout_ms) as new_page_info:
            await click_action()

        new_page = await new_page_info.value
        await new_page.wait_for_load_state("domcontentloaded")
        self._page = new_page
        logger.info("Switched to new tab: %s", new_page.url)
        return new_page

    async def switch_to_portal_page(self) -> None:
        """Switch back to the portal tab (saved earlier via save_as_portal_page)."""
        if self._portal_page and not self._portal_page.is_closed():
            self._page = self._portal_page
            await self._page.bring_to_front()
            logger.debug("Switched back to portal tab")
        elif self._context and self._context.pages:
            # Fallback: use the first open page
            self._page = self._context.pages[0]
            await self._page.bring_to_front()
            logger.warning("Portal page lost, fell back to first open tab")
        else:
            raise RuntimeError("No pages available to switch to")

    async def close_current_page(self) -> None:
        """Close the current tab (only if it is not the portal tab)."""
        if self._page and self._page != self._portal_page:
            url = self._page.url
            await self._page.close()
            logger.info("Closed course tab: %s", url)
            self._page = None

    @property
    def context(self) -> BrowserContext:
        """The underlying Playwright BrowserContext."""
        if not self._context:
            raise RuntimeError("Browser not started. Call start() first.")
        return self._context

    async def navigate(self, url: str, wait_until: str = "domcontentloaded") -> None:
        """Navigate to a URL and wait for initial load."""
        if not self._page:
            raise RuntimeError("Browser not started. Call start() first.")
        logger.debug(f"Navigating to {url}")
        await self._page.goto(url, wait_until=wait_until, timeout=60000)

    async def wait_for_stable_page(self, timeout_ms: Optional[int] = None) -> None:
        """Wait for page to be fully loaded and stable.

        Strategy:
        1. Wait for network idle (no pending requests for 500ms)
        2. Wait for DOM mutation quiescence (no changes for configured period)
        """
        if not self._page:
            return

        timeout = timeout_ms or self.config.stable_wait_ms
        quiet_ms = self.config.mutation_quiet_ms

        # Cap the networkidle wait separately from the overall ceiling.
        # Cornerstone heartbeats keep the network busy, so this rarely
        # resolves cleanly — the MutationObserver block below is the
        # actual stability signal. Don't let networkidle dominate.
        networkidle_timeout = min(timeout, 5000)
        try:
            await self._page.wait_for_load_state(
                "networkidle", timeout=networkidle_timeout
            )
        except Exception:
            logger.debug("Network idle timeout — continuing with DOM check")

        # Inject MutationObserver to wait for DOM stability
        try:
            await self._page.evaluate(
                f"""() => new Promise(resolve => {{
                    let timer;
                    const observer = new MutationObserver(() => {{
                        clearTimeout(timer);
                        timer = setTimeout(() => {{
                            observer.disconnect();
                            resolve();
                        }}, {quiet_ms});
                    }});
                    observer.observe(document.body, {{
                        childList: true,
                        subtree: true,
                        attributes: true
                    }});
                    // Fallback: resolve after 2x quiet period even if mutations continue
                    timer = setTimeout(() => {{
                        observer.disconnect();
                        resolve();
                    }}, {quiet_ms * 2});
                }})""",
                timeout=timeout,
            )
        except Exception:
            logger.debug("DOM stability check timed out — proceeding anyway")

    async def wait_for_content_ready(
        self,
        selectors: "SelectorProfile",
        timeout_ms: int = 30000,
    ) -> None:
        """Wait for content to actually render (spinner gone, page counter valid).

        Complements wait_for_stable_page() which only checks network/DOM quiescence.
        All checks are best-effort — if selectors don't match, we fall through.
        """
        if not self._page:
            return

        # 1. Wait for loading spinner to disappear
        spinner_sel = selectors.get("loading_spinner")
        if spinner_sel:
            try:
                spinner = self._page.locator(spinner_sel).first
                if await spinner.is_visible():
                    logger.debug("Waiting for loading spinner to disappear")
                    await spinner.wait_for(state="hidden", timeout=timeout_ms)
            except Exception:
                pass  # No spinner found or timeout — continue

        # 2. Wait for page indicator to show a real page number (not "- of 10")
        indicator_sel = selectors.get("page_indicator")
        if indicator_sel:
            try:
                await self._page.locator(indicator_sel).first.wait_for(
                    state="visible", timeout=5000,
                )
                await self._page.wait_for_function(
                    """(sel) => {
                        const el = document.querySelector(sel);
                        return el && /\\d+\\s+(of|\\/)\\s+\\d+/.test(el.textContent);
                    }""",
                    indicator_sel,
                    timeout=timeout_ms,
                )
                logger.debug("Page indicator shows valid page number")
            except Exception:
                pass  # Indicator not found — fall through

    async def _safe_evaluate(self, expression: str, *args, retries: int = 1):
        """page.evaluate with one retry when the execution context is destroyed
        by a concurrent navigation (e.g. Cornerstone player re-render).
        """
        for attempt in range(retries + 1):
            try:
                if args:
                    return await self._page.evaluate(expression, *args)
                return await self._page.evaluate(expression)
            except Exception as e:
                msg = str(e)
                transient = (
                    "Execution context was destroyed" in msg
                    or "Target closed" in msg
                    or "context destroyed" in msg.lower()
                )
                if attempt < retries and transient:
                    logger.debug("evaluate() hit navigation race, retrying: %s", msg)
                    try:
                        await self._page.wait_for_load_state(
                            "domcontentloaded", timeout=10000
                        )
                    except Exception:
                        pass
                    continue
                raise

    async def get_page_height(self) -> int:
        """Get the full scrollable height of the page."""
        if not self._page:
            return 0
        return await self._safe_evaluate(
            "() => document.documentElement.scrollHeight"
        )

    async def get_viewport_height(self) -> int:
        """Get the viewport height."""
        if not self._page:
            return 0
        return await self._safe_evaluate("() => window.innerHeight")

    async def scroll_to(self, y: int) -> None:
        """Scroll to a specific Y position."""
        if self._page:
            await self._safe_evaluate(f"window.scrollTo(0, {y})")
            await asyncio.sleep(0.3)  # brief pause for render

    async def get_current_url(self) -> str:
        """Get the current page URL."""
        return self._page.url if self._page else ""

    @property
    def page(self) -> Page:
        """Current active Playwright page."""
        if not self._page:
            raise RuntimeError("Browser not started. Call start() first.")
        return self._page

    async def close(self) -> None:
        """Clean shutdown of browser and Playwright."""
        if self._context:
            await self._context.close()
            self._context = None
            self._page = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        logger.info("Browser session closed")
