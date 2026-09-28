"""A deliberately polite HTTP client.

Three properties matter more here than speed:

1. **robots.txt is enforced, not consulted.** Every request is checked against
   the origin's rules before it is sent, and a disallowed URL raises rather
   than warns. Sources are opted in explicitly in ``config/targets.yaml``; a
   site that has not been vetted cannot be fetched by accident.
2. **Crawl-delay is honoured per origin.** Loba Chemie publishes
   ``Crawl-delay: 5``, so this client waits five seconds between requests to
   that host even though nothing forces it to.
3. **Responses are cached on disk.** Re-running the pipeline during
   development costs the suppliers nothing after the first pass, which is the
   single biggest reduction in load a scraper can make.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import threading
import time
import urllib.robotparser
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

DEFAULT_UA = (
    "chem-catalog-resolver/0.1 (portfolio data-reconciliation project; "
    "+https://github.com/Az-otrope/chem-catalog-resolver) python-requests"
)


class RobotsDisallowed(RuntimeError):
    """Raised when a URL is blocked by the origin's robots.txt."""


class SourceNotAllowed(RuntimeError):
    """Raised when a host has not been explicitly opted into in config."""


@dataclass
class FetchResult:
    url: str
    status: int
    text: str
    from_cache: bool
    content: bytes = b""

    def json(self):
        return json.loads(self.text)


class PoliteSession:
    def __init__(
        self,
        cache_dir: Path | str = "data/cache",
        user_agent: str = DEFAULT_UA,
        default_delay: float = 2.0,
        allowed_hosts: set[str] | None = None,
        respect_robots: bool = True,
        jitter: float = 0.4,
        timeout: float = 30.0,
        max_retries: int = 3,
        robots_authority: dict[str, str] | None = None,
        robots_overrides: list[dict] | None = None,
        host_delays: dict[str, float] | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.user_agent = user_agent
        self.default_delay = default_delay
        self.allowed_hosts = allowed_hosts
        self.respect_robots = respect_robots
        self.jitter = jitter
        self.timeout = timeout
        self.max_retries = max_retries
        # Maps an API host to the storefront host whose robots.txt also
        # governs it. An API subdomain that serves no robots.txt of its own is
        # unrestricted by the letter of the standard, but it is serving the
        # same operator's catalogue, so the storefront's rules are applied on
        # top of whatever the API host says.
        self.robots_authority = {
            k.lower(): v.lower() for k, v in (robots_authority or {}).items()
        }
        # Per-host floor, overriding default_delay. Used where a source
        # publishes its own rate policy — PubChem documents 5 requests/second,
        # so holding it to the 2s global floor would be slow for no benefit to
        # anyone. Never used to go faster than a declared Crawl-delay.
        self.host_delays = {k.lower(): float(v) for k, v in (host_delays or {}).items()}
        # Narrow, declared exceptions to robots.txt. See _overridden().
        self.robots_overrides = robots_overrides or []
        for override in self.robots_overrides:
            missing = {"host", "path_prefix", "justification"} - set(override)
            if missing:
                raise ValueError(
                    f"robots override is missing required keys {sorted(missing)}: {override}"
                )

        self._session = requests.Session()
        self._session.headers.update({"User-Agent": user_agent})
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._last_hit: dict[str, float] = {}
        self._lock = threading.Lock()
        self.stats = {
            "requests": 0,
            "cache_hits": 0,
            "blocked": 0,
            "errors": 0,
            "robots_overrides_used": 0,
        }

    # -- policy ---------------------------------------------------------

    def _robots_for(self, origin: str) -> urllib.robotparser.RobotFileParser | None:
        """Fetch and parse an origin's robots.txt.

        Returns None to mean "treat this origin as disallowed". The status-code
        handling follows RFC 9309 with one deliberate exception:

        * **404/410** — no robots.txt exists, so no restrictions are asserted.
          The RFC says to proceed, and this does.
        * **401/403** — the RFC also permits proceeding, and this deliberately
          does not. In practice a site that refuses to serve robots.txt to a
          non-browser client is running a bot wall, and getting past it would
          mean pretending to be something we are not. Refusing here is
          stricter than the standard requires, on purpose.
        * **5xx / network failure** — unavailable, so disallowed per the RFC.
        """
        if origin in self._robots:
            return self._robots[origin]

        parser: urllib.robotparser.RobotFileParser | None
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(f"{origin}/robots.txt")
        try:
            resp = self._session.get(f"{origin}/robots.txt", timeout=self.timeout)
            if resp.status_code == 200:
                parser.parse(resp.text.splitlines())
            elif resp.status_code in (404, 410):
                log.info("robots.txt absent for %s (%s); no restrictions asserted",
                         origin, resp.status_code)
                parser.parse([])  # empty ruleset == allow all
            elif resp.status_code in (401, 403):
                log.warning(
                    "robots.txt for %s returned %s — refusing automated clients; "
                    "treating origin as disallowed", origin, resp.status_code
                )
                parser = None
            else:
                log.warning("robots.txt for %s returned %s; treating as disallowed",
                            origin, resp.status_code)
                parser = None
        except requests.RequestException as exc:
            log.warning("robots.txt for %s unreachable: %s", origin, exc)
            parser = None

        self._robots[origin] = parser
        return parser

    def _max_declared_delay(self, parser: urllib.robotparser.RobotFileParser) -> float:
        """The largest Crawl-delay declared anywhere in the file.

        Deliberately over-compliant. Loba Chemie declares ``Crawl-delay: 5``
        in a group naming specific crawler user-agents, not in the wildcard
        group — so by the letter of the standard it does not bind this client.
        It is still the operator telling anyone automated what pace they
        consider acceptable, and there is no good reason to read past that on
        a technicality.
        """
        delays = []
        for entry in list(getattr(parser, "entries", [])) + (
            [parser.default_entry] if getattr(parser, "default_entry", None) else []
        ):
            value = getattr(entry, "delay", None)
            if value:
                delays.append(float(value))
        return max(delays, default=0.0)

    def crawl_delay_for(self, origin: str) -> float:
        host = urlparse(origin).netloc.lower()
        floor = self.host_delays.get(host, self.default_delay)
        parser = self._robots_for(origin)
        if parser is None:
            return floor
        try:
            binding = parser.crawl_delay(self.user_agent) or parser.crawl_delay("*")
        except AttributeError:
            binding = None
        declared = max(float(binding or 0), self._max_declared_delay(parser))
        # A declared Crawl-delay always wins over a configured floor, so a
        # per-host setting can slow us down but never speed us past the site.
        return max(declared, floor)

    def _overridden(self, host: str, path: str) -> dict | None:
        """Find a declared exception covering this host and path prefix.

        Overrides exist for one situation: a site publishes a documented
        programmatic API *and* a robots.txt that disallows it, because
        robots.txt is addressed to indexing crawlers and nobody updated it for
        API clients. PubChem is exactly this case — PUG-REST is the documented
        public interface, with its own published rate policy, while the
        wildcard robots group disallows ``/rest/pug/`` alongside ``/widget``
        and ``/deposit/``.

        An override is not a way to get past a site that does not want
        automated access. It never applies to a host that refused to serve
        robots.txt, it is scoped to one path prefix, it must carry a written
        justification, and every use is counted in ``stats`` so a run can be
        audited afterwards. Anything that needed disguise or evasion would be
        a declined target instead.
        """
        for override in self.robots_overrides:
            if override["host"].lower() != host:
                continue
            if path.startswith(override["path_prefix"]):
                return override
        return None

    def check(self, url: str) -> None:
        """Raise unless this URL may be fetched."""
        parts = urlparse(url)
        host = parts.netloc.lower()
        origin = f"{parts.scheme}://{parts.netloc}"

        if self.allowed_hosts is not None and host not in self.allowed_hosts:
            raise SourceNotAllowed(
                f"{host} is not in the configured allow-list. Add it to "
                f"config/targets.yaml only after checking its robots.txt and terms."
            )

        if not self.respect_robots:
            return

        parser = self._robots_for(origin)
        if parser is None:
            raise RobotsDisallowed(
                f"{origin} did not serve a usable robots.txt; treating as disallowed."
            )
        if not parser.can_fetch(self.user_agent, url):
            override = self._overridden(host, parts.path)
            if override is None:
                raise RobotsDisallowed(f"robots.txt disallows {url}")
            self.stats["robots_overrides_used"] += 1
            log.debug(
                "robots override applied for %s (%s)", url, override["justification"]
            )

        authority = self.robots_authority.get(host)
        if authority:
            auth_parser = self._robots_for(f"{parts.scheme}://{authority}")
            if auth_parser is None:
                raise RobotsDisallowed(
                    f"governing storefront {authority} did not serve a usable robots.txt"
                )
            mirrored = url.replace(parts.netloc, authority, 1)
            if not auth_parser.can_fetch(self.user_agent, mirrored):
                raise RobotsDisallowed(
                    f"governing storefront {authority} disallows the equivalent path {mirrored}"
                )

    # -- fetching -------------------------------------------------------

    def _cache_path(self, url: str, suffix: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]
        host = urlparse(url).netloc.replace(":", "_")
        bucket = self.cache_dir / host
        bucket.mkdir(parents=True, exist_ok=True)
        return bucket / f"{digest}{suffix}"

    def _throttle(self, origin: str) -> None:
        delay = self.crawl_delay_for(origin)
        with self._lock:
            last = self._last_hit.get(origin, 0.0)
            wait = delay - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait + random.uniform(0, self.jitter))
            self._last_hit[origin] = time.monotonic()

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        binary: bool = False,
        use_cache: bool = True,
    ) -> FetchResult:
        suffix = ".bin" if binary else ".txt"
        cache_file = self._cache_path(url, suffix)

        if use_cache and cache_file.exists():
            self.stats["cache_hits"] += 1
            raw = cache_file.read_bytes()
            return FetchResult(
                url=url,
                status=200,
                text="" if binary else raw.decode("utf-8", errors="replace"),
                from_cache=True,
                content=raw,
            )

        try:
            self.check(url)
        except (RobotsDisallowed, SourceNotAllowed):
            self.stats["blocked"] += 1
            raise

        parts = urlparse(url)
        origin = f"{parts.scheme}://{parts.netloc}"

        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            self._throttle(origin)
            try:
                resp = self._session.get(
                    url, headers=headers or {}, timeout=self.timeout
                )
                self.stats["requests"] += 1
            except requests.RequestException as exc:
                last_exc = exc
                time.sleep(2**attempt)
                continue

            if resp.status_code in (429, 503):
                # Back off hard; these mean we are being told to slow down.
                retry_after = float(resp.headers.get("Retry-After") or 0)
                time.sleep(max(retry_after, 5 * (attempt + 1)))
                last_exc = RuntimeError(f"{resp.status_code} from {url}")
                continue

            if resp.status_code >= 400:
                self.stats["errors"] += 1
                return FetchResult(
                    url=url,
                    status=resp.status_code,
                    text=resp.text if not binary else "",
                    from_cache=False,
                    content=resp.content,
                )

            if use_cache:
                cache_file.write_bytes(resp.content)
            return FetchResult(
                url=url,
                status=resp.status_code,
                text="" if binary else resp.text,
                from_cache=False,
                content=resp.content,
            )

        self.stats["errors"] += 1
        raise RuntimeError(f"giving up on {url}: {last_exc}")
