import hashlib
import ipaddress
import logging
import socket
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

log = logging.getLogger(__name__)
MAX_PAGE_BYTES = 5_000_000
MAX_REDIRECTS = 10


@dataclass
class ScrapeResult:
    ok: bool
    status: str
    message: str
    html: str = ""
    title: str = ""
    http_status: int = 0
    screenshot_path: str = ""
    final_url: str = ""


def canonicalize_url(value):
    value = (value or "").strip()
    if not value or len(value) > 2048:
        raise ValueError("Enter a valid public HTTP or HTTPS URL.")

    try:
        parts = urlsplit(value)
        scheme = parts.scheme.casefold()
        hostname = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise ValueError("The URL is malformed.") from exc

    if scheme not in {"http", "https"} or not hostname:
        raise ValueError("Only public HTTP and HTTPS URLs are allowed.")

    if parts.username or parts.password:
        raise ValueError("URLs containing usernames or passwords are not allowed.")

    if parts.fragment:
        parts = parts._replace(fragment="")

    if port is not None and port not in {80, 443}:
        raise ValueError("Only the standard HTTP and HTTPS ports are allowed.")

    host = hostname.rstrip(".").casefold()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        raise ValueError("Local and localhost URLs are not allowed.")

    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            records = socket.getaddrinfo(host, port or (443 if scheme == "https" else 80), type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ValueError("The URL hostname could not be resolved.") from exc
        addresses = set()
        for record in records:
            try:
                addresses.add(ipaddress.ip_address(record[4][0].split("%")[0]))
            except ValueError:
                continue

    if not addresses or any(
        not address.is_global
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    for address in addresses):
        raise ValueError("The URL must resolve only to public IP addresses.")

    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"

    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def hash_content(value):
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _is_block_page(title, text, status_code):
    if status_code in {401, 403, 407, 429}:
        return True

    combined = f"{title}\n{text[:20000]}".casefold()
    markers = (
        "verify you are human",
        "checking your browser",
        "captcha",
        "access denied",
        "unusual traffic",
        "automated requests",
        "security challenge",
        "request blocked",
        "bot detection",
    )
    return any(marker in combined for marker in markers)


def scrape_url(url, screenshot_dir, timeout_ms=25000, retries=2):
    try:
        canonical = canonicalize_url(url)
    except ValueError as exc:
        return ScrapeResult(False, "BLOCKED", str(exc))

    timeout_ms = max(5000, min(int(timeout_ms), 90000))
    screenshot_root = Path(screenshot_dir).resolve()
    screenshot_root.mkdir(parents=True, exist_ok=True)
    last_error = "The page could not be fetched."

    try:
        retries = max(0, min(int(retries), 5))
    except (TypeError, ValueError):
        retries = 2

    for attempt in range(retries + 1):
        browser = None
        context = None
        request_count = {"redirects": 0, "blocked": False}
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(
                    viewport={"width": 1440, "height": 1000},
                    device_scale_factor=1,
                    java_script_enabled=True,
                    ignore_https_errors=False,
                )
                page = context.new_page()
                page.set_default_navigation_timeout(timeout_ms)
                page.set_default_timeout(timeout_ms)
                def guard(route):
                    try:
                        request_scheme = urlsplit(route.request.url).scheme.casefold()
                        if request_scheme in {"about", "blob", "data"}:
                            route.continue_()
                            return
                        canonicalize_url(route.request.url)
                        if route.request.is_navigation_request() and route.request.redirected_from:
                            request_count["redirects"] += 1
                            if request_count["redirects"] > MAX_REDIRECTS:
                                route.abort()
                                return
                        route.continue_()
                    except Exception:
                        request_count["blocked"] = True
                        route.abort()

                page.route("**/*", guard)
                response = page.goto(canonical, wait_until="domcontentloaded", timeout=timeout_ms)

                if response is None:
                    raise RuntimeError("The website did not return a navigation response.")

                try:
                    final_url = canonicalize_url(page.url)
                except ValueError as exc:
                    return ScrapeResult(
                        False,
                        "BLOCKED",
                        f"The page redirected to an unsafe URL: {exc}",
                    )
                status_code = response.status

                if status_code in {401, 403, 407, 429}:
                    return ScrapeResult(
                        False,
                        "BLOCKED",
                        f"The website returned HTTP {status_code}. It may require access or be rate-limiting checks.",
                        http_status=status_code,
                        final_url=final_url,
                    )

                if status_code >= 500:
                    raise RuntimeError(f"The website returned HTTP {status_code}.")

                if status_code < 200 or status_code >= 300:
                    return ScrapeResult(
                        False,
                        "ERROR",
                        f"The website returned HTTP {status_code}.",
                        http_status=status_code,
                        final_url=final_url,
                    )

                try:
                    page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 5000))
                except PlaywrightTimeoutError:
                    pass

                page.wait_for_timeout(300)
                title = page.title()[:500]
                html = page.content()

                if len(html.encode("utf-8", errors="replace")) > MAX_PAGE_BYTES:
                    return ScrapeResult(
                        False,
                        "ERROR",
                        "The rendered page exceeded the 5 MB processing limit.",
                        http_status=status_code,
                        final_url=final_url,
                    )

                body_text = page.locator("body").inner_text(timeout=5000) if page.locator("body").count() else ""
                if _is_block_page(title, body_text, status_code):
                    return ScrapeResult(
                        False,
                        "BLOCKED",
                        "The website appears to show a CAPTCHA, access-denied page, or bot challenge.",
                        http_status=status_code,
                        final_url=final_url,
                    )

                screenshot_path = screenshot_root / f"{uuid.uuid4().hex}.png"
                page.screenshot(path=str(screenshot_path), full_page=True, animations="disabled")

                return ScrapeResult(
                    True,
                    "OK",
                    "",
                    html=html,
                    title=title,
                    http_status=status_code,
                    screenshot_path=str(screenshot_path),
                    final_url=final_url,
                )

        except (PlaywrightTimeoutError, PlaywrightError, OSError, RuntimeError, ValueError) as exc:
            if request_count["blocked"]:
                return ScrapeResult(
                    False,
                    "BLOCKED",
                    "The page attempted to load a non-public or unsafe resource.",
                )
            last_error = str(exc)[:500] or exc.__class__.__name__
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
        except Exception as exc:
            log.exception("Unexpected browser fetch error for %s", canonical)
            last_error = str(exc)[:500] or "Unexpected browser error."
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass

    return ScrapeResult(False, "ERROR", last_error)