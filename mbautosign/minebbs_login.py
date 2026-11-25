from __future__ import annotations

from urllib.parse import urlsplit

from playwright.sync_api import BrowserContext, Error as BrowserError, Page, Response, Route, TimeoutError

from .runlog import RunLog


HOME = "https://www.minebbs.com/"
LOGIN_LINK = 'a.p-navgroup-link--logIn[data-xf-click="overlay"]'
LOGIN_FORM = '.overlay form:has(input[name="login"]):has(input[name="password"][type="password"])'
CHALLENGE = '#slg-box, [data-sitekey], iframe[src*="captcha"], iframe[src*="challenges.cloudflare.com"]'


def session_state(page: Page) -> str:
    return page.evaluate("""({home, linkSelector}) => {
        if (location.href !== home) return 'unexpected_page';
        if (document.querySelector('#slg-box')) return 'security_check';
        const root = document.documentElement;
        if (root?.id !== 'XF') return 'unknown';
        const flag = root.getAttribute('data-logged-in');
        const validLink = [...document.querySelectorAll(linkSelector)].some(a => {
            const url = new URL(a.href);
            return url.origin === location.origin && ['/login', '/login/'].includes(url.pathname);
        });
        if (flag === 'false' && validLink && !document.querySelector('.p-navgroup-link--user')) {
            return 'signed_out';
        }
        if (flag === 'true' && !validLink) return 'signed_in';
        return 'unknown';
    }""", {"home": HOME, "linkSelector": LOGIN_LINK})


def navigate_home(page: Page, challenge_wait_ms: int = 0, *, log: RunLog | None = None) -> tuple[int | None, str, int | None]:
    latest_status = None

    def record_home_response(response: Response) -> None:
        nonlocal latest_status
        request = response.request
        if response.url == HOME and request.is_navigation_request():
            try:
                if request.frame == page.main_frame:
                    latest_status = response.status
            except BrowserError:
                pass  # Some navigation responses have no associated frame yet.

    if challenge_wait_ms:
        page.on("response", record_home_response)
    try:
        response = page.goto(HOME, wait_until="domcontentloaded")
        initial_status = response.status if response else None
        state = "security_check" if initial_status == 468 else session_state(page)
        if state == "security_check" and challenge_wait_ms:
            # The site may navigate to the same URL; a URL change alone cannot signal completion.
            if log:
                log.event("security_check_wait_started", initial_status=initial_status, timeout_ms=challenge_wait_ms)
            try:
                page.wait_for_function("""home => location.href === home &&
                    document.readyState !== 'loading' && document.documentElement?.id === 'XF' &&
                    !document.querySelector('#slg-box')""", arg=HOME, timeout=challenge_wait_ms)
            except TimeoutError:
                pass
            else:
                if latest_status == 200:
                    final_state = session_state(page)
                    if log:
                        log.event("security_check_finished", home_status=200, session_state=final_state)
                    return 200, final_state, initial_status
            if log:
                log.event("security_check_unresolved", level="warning", initial_status=initial_status)
        return initial_status, state, initial_status
    finally:
        if challenge_wait_ms:
            page.remove_listener("response", record_home_response)


def is_login_endpoint(url: str) -> bool:
    endpoint = urlsplit(url)
    return (endpoint.scheme == "https" and endpoint.netloc == "www.minebbs.com"
            and endpoint.path in {"/login/login", "/login/login/"})


def restore_login(context: BrowserContext, username: str, password: str, timeout_ms: int,
                  *, challenge_wait_ms: int = 0, log: RunLog | None = None) -> dict:
    page = context.new_page()
    page.set_default_timeout(timeout_ms)
    page.set_default_navigation_timeout(timeout_ms)
    attempts = 0
    armed = False
    home_status = None
    initial_status = None
    phase = "login_home"

    def progress(stage: str, **details: object) -> None:
        nonlocal phase
        phase = stage
        if log:
            log.event("login_phase", phase=phase, **details)

    def guard_login_post(route: Route) -> None:
        nonlocal attempts
        request = route.request
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            route.fallback()
        elif armed and attempts == 0 and request.method == "POST" and is_login_endpoint(request.url):
            attempts += 1
            if log:
                log.event("login_request_allowed", login_post_count=attempts)
            route.fallback()
        else:
            route.abort()

    def result(state: str, **details: object) -> dict:
        return {"login_result": state, "login_phase": phase, "login_post_count": attempts,
                "login_home_status": home_status, "login_home_initial_status": initial_status, **details}

    try:
        cdp = context.new_cdp_session(page)
        cdp.send("Network.setBypassServiceWorker", {"bypass": True})
        progress("login_home")
        home_status, state, initial_status = navigate_home(page, challenge_wait_ms, log=log)
        if log:
            log.event("login_home_checked", home_status=home_status, initial_status=initial_status, session_state=state)
        if home_status != 200:
            return result("security_check" if state == "security_check" else "homepage_failed")
        if state == "signed_in":
            return result("already_logged_in")
        if state != "signed_out":
            return result(state)
        if not username or not password:
            return result("credentials_missing")

        progress("open_login_overlay")
        link = page.locator(f"{LOGIN_LINK}:visible")
        if link.count() != 1:
            return result("login_link_ambiguous")
        link_url = urlsplit(link.evaluate("a => a.href"))
        if (link_url.scheme, link_url.netloc) != ("https", "www.minebbs.com") or link_url.path not in {"/login", "/login/"}:
            return result("unexpected_login_link")
        link.click()
        form = page.locator(f"{LOGIN_FORM}:visible")
        if page.locator(CHALLENGE).count():
            return result("verification_required")
        form.first.wait_for()
        if form.count() != 1 or page.url != HOME:
            return result("login_form_ambiguous")
        if page.locator(CHALLENGE).count():
            return result("verification_required")

        def safe_form() -> bool:
            return (page.url == HOME and form.evaluate("f => f.method === 'post' && !f.target")
                    and is_login_endpoint(form.evaluate("f => f.action")))

        if not safe_form():
            return result("unexpected_login_form")
        name = form.locator('input[name="login"]:visible')
        secret = form.locator('input[name="password"][type="password"]:visible')
        button = form.locator('button[type="submit"]:visible, input[type="submit"]:visible')
        if any(control.count() != 1 for control in (name, secret, button)):
            return result("login_controls_ambiguous")

        def safe_submitter() -> bool:
            return safe_form() and button.evaluate("""b =>
                b.form === b.closest('form') && !b.matches('[formaction],[formmethod],[formtarget]')
            """)

        if not safe_submitter():
            return result("unexpected_login_form")

        progress("fill_login_form")
        page.route("**/*", guard_login_post)
        name.fill(username)
        secret.fill(password)
        remember = form.locator('input[type="checkbox"][name="remember"]:visible')
        if remember.count() == 1:
            remember.check()
        if not safe_submitter() or page.locator(CHALLENGE).count():
            return result("login_form_changed")

        progress("submit_login_form")
        armed = True
        with page.expect_response(lambda r: r.request.method == "POST" and is_login_endpoint(r.url)) as pending:
            button.click()
        login_response = pending.value
        if log:
            log.event("login_response", login_http_status=login_response.status, login_post_count=attempts)
        if not 200 <= login_response.status < 400:
            return result("login_rejected", login_http_status=login_response.status)
        return result("submitted", login_http_status=login_response.status)
    except TimeoutError as exc:
        if log:
            log.exception("login_timed_out", exc, phase=phase, login_post_count=attempts)
        return result("login_outcome_unknown" if attempts else "login_ui_not_ready")
    except Exception as exc:
        if log:
            log.exception("login_failed", exc, phase=phase, login_post_count=attempts)
        return result("login_outcome_unknown" if attempts else "login_failed")
    finally:
        armed = False
        try:
            page.close()
        except BrowserError as exc:
            if log:
                log.exception("login_page_cleanup_failed", exc)
