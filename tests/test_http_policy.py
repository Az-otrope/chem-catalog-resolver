"""Access-policy behaviour.

These assertions are about conduct rather than correctness, which is why they
are tested at all: the policy is the part of this project that a reviewer is
entitled to distrust, and "we respect robots.txt" is worth exactly as much as
the test that proves it.
"""

import pytest

from chemspec.http import PoliteSession, RobotsDisallowed, SourceNotAllowed


class FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text
        self.content = text.encode()
        self.headers = {}


def session_with(robots_text, status=200, **kwargs):
    s = PoliteSession(cache_dir=kwargs.pop("cache_dir"), default_delay=0, **kwargs)
    s._session.get = lambda url, **kw: FakeResponse(status, robots_text)  # type: ignore[assignment]
    return s


class TestAllowList:
    def test_unlisted_host_is_refused_before_any_request(self, tmp_path):
        s = PoliteSession(cache_dir=tmp_path, allowed_hosts={"example.com"})
        with pytest.raises(SourceNotAllowed):
            s.check("https://not-listed.example.org/page")


class TestRobotsStatusHandling:
    def test_404_means_no_restrictions(self, tmp_path):
        s = session_with("", status=404, cache_dir=tmp_path)
        s.check("https://example.com/anything")  # must not raise

    def test_403_is_treated_as_refusal(self, tmp_path):
        # Stricter than RFC 9309 permits, deliberately: a site that will not
        # serve robots.txt to a non-browser client is running a bot wall.
        s = session_with("", status=403, cache_dir=tmp_path)
        with pytest.raises(RobotsDisallowed):
            s.check("https://example.com/anything")

    def test_5xx_is_treated_as_disallowed(self, tmp_path):
        s = session_with("", status=503, cache_dir=tmp_path)
        with pytest.raises(RobotsDisallowed):
            s.check("https://example.com/anything")

    def test_disallowed_path_raises(self, tmp_path):
        s = session_with("User-agent: *\nDisallow: /private/", cache_dir=tmp_path)
        s.check("https://example.com/public/ok")
        with pytest.raises(RobotsDisallowed):
            s.check("https://example.com/private/secret")


class TestCrawlDelay:
    def test_delay_declared_for_other_agents_is_still_honoured(self, tmp_path):
        # Loba Chemie's real shape: Crawl-delay sits in a named-agent group,
        # not the wildcard one, so it does not bind us. Honoured anyway.
        robots = (
            "User-agent: *\nAllow: /\n\n"
            "User-agent: ClaudeBot\nUser-agent: GPTBot\nCrawl-delay: 5\n"
        )
        s = session_with(robots, cache_dir=tmp_path)
        assert s.crawl_delay_for("https://example.com") == 5.0

    def test_configured_floor_never_outruns_a_declared_delay(self, tmp_path):
        robots = "User-agent: *\nCrawl-delay: 9\n"
        s = session_with(robots, cache_dir=tmp_path, host_delays={"example.com": 0.1})
        assert s.crawl_delay_for("https://example.com") == 9.0

    def test_per_host_floor_applies_when_nothing_is_declared(self, tmp_path):
        s = session_with("User-agent: *\nAllow: /\n", cache_dir=tmp_path,
                         host_delays={"example.com": 0.25})
        assert s.crawl_delay_for("https://example.com") == 0.25


class TestRobotsOverrides:
    ROBOTS = "User-agent: *\nDisallow: /rest/pug/\n"

    def test_override_permits_only_its_declared_prefix(self, tmp_path):
        s = session_with(
            self.ROBOTS,
            cache_dir=tmp_path,
            robots_overrides=[{
                "host": "example.com",
                "path_prefix": "/rest/pug/",
                "justification": "documented public API",
            }],
        )
        s.check("https://example.com/rest/pug/compound/name/water/JSON")
        assert s.stats["robots_overrides_used"] == 1

    def test_without_the_override_the_same_url_is_refused(self, tmp_path):
        s = session_with(self.ROBOTS, cache_dir=tmp_path)
        with pytest.raises(RobotsDisallowed):
            s.check("https://example.com/rest/pug/compound/name/water/JSON")

    def test_override_does_not_leak_to_other_paths(self, tmp_path):
        s = session_with(
            "User-agent: *\nDisallow: /rest/pug/\nDisallow: /admin/\n",
            cache_dir=tmp_path,
            robots_overrides=[{
                "host": "example.com",
                "path_prefix": "/rest/pug/",
                "justification": "documented public API",
            }],
        )
        with pytest.raises(RobotsDisallowed):
            s.check("https://example.com/admin/users")

    def test_override_does_not_leak_to_other_hosts(self, tmp_path):
        s = session_with(
            self.ROBOTS,
            cache_dir=tmp_path,
            robots_overrides=[{
                "host": "example.com",
                "path_prefix": "/rest/pug/",
                "justification": "documented public API",
            }],
        )
        with pytest.raises(RobotsDisallowed):
            s.check("https://other.example.net/rest/pug/thing")

    def test_override_cannot_rescue_a_host_that_refused_robots(self, tmp_path):
        # The escape hatch must not become a way past a bot wall.
        s = session_with(
            "", status=403, cache_dir=tmp_path,
            robots_overrides=[{
                "host": "example.com",
                "path_prefix": "/",
                "justification": "should not help",
            }],
        )
        with pytest.raises(RobotsDisallowed):
            s.check("https://example.com/anything")

    def test_override_without_justification_is_rejected_at_construction(self, tmp_path):
        with pytest.raises(ValueError, match="justification"):
            PoliteSession(
                cache_dir=tmp_path,
                robots_overrides=[{"host": "example.com", "path_prefix": "/x"}],
            )
