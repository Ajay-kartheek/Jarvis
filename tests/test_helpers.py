from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fakes import FakeHttpError

from jarvis import ratelimit, textutil, timeutil


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


# --- pacing ----------------------------------------------------------------


def test_limiter_keeps_the_average_at_the_configured_rate():
    clock = Clock()
    limiter = ratelimit.RateLimiter(75, clock=clock, sleep=clock.sleep)

    for _ in range(40):  # 40 batches of 15 message fetches, 5 units each
        limiter.acquire(75)

    assert clock.now == pytest.approx(39.0)  # 3,000 units took 39+ seconds: under 80 a second
    assert 3000 / (clock.now + 1.0) <= 75


def test_limiter_does_not_wait_when_idle():
    clock = Clock()
    limiter = ratelimit.RateLimiter(10, clock=clock, sleep=clock.sleep)

    limiter.acquire(5)
    clock.now += 60
    limiter.acquire(5)

    assert clock.slept == []


def test_limiter_rejects_a_bad_rate():
    with pytest.raises(ValueError):
        ratelimit.RateLimiter(0)


# --- retries ---------------------------------------------------------------


@pytest.mark.parametrize(
    "error, transient",
    [
        (FakeHttpError(429), True),
        (FakeHttpError(500), True),
        (FakeHttpError(503), True),
        (FakeHttpError(403, "rateLimitExceeded"), True),
        (FakeHttpError(403, "userRateLimitExceeded"), True),
        (FakeHttpError(403, "accessNotConfigured"), False),
        (FakeHttpError(404), False),
        (FakeHttpError(400), False),
        (TimeoutError(), True),
        (ConnectionResetError(), True),
        (ValueError("bug"), False),
    ],
)
def test_which_errors_are_retried(error, transient):
    assert ratelimit.is_transient(error) is transient


def test_retry_recovers_from_transient_errors():
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise FakeHttpError(503)
        return "ok"

    assert ratelimit.call_with_retry(flaky, sleep=lambda s: None) == "ok"
    assert len(attempts) == 3


def test_retry_gives_up_and_does_not_retry_real_errors():
    calls = []

    def always_busy():
        calls.append(1)
        raise FakeHttpError(429)

    with pytest.raises(FakeHttpError):
        ratelimit.call_with_retry(always_busy, attempts=3, sleep=lambda s: None)
    assert len(calls) == 3

    def forbidden():
        calls.append(1)
        raise FakeHttpError(403, "accessNotConfigured")

    calls.clear()
    with pytest.raises(FakeHttpError):
        ratelimit.call_with_retry(forbidden, sleep=lambda s: None)
    assert len(calls) == 1


def test_error_reason_reads_googles_reason():
    assert ratelimit.error_reason(FakeHttpError(403, "accessNotConfigured")) == "accessNotConfigured"
    assert ratelimit.error_reason(ValueError("x")) == ""


# --- text ------------------------------------------------------------------


def test_html_to_text_drops_markup_scripts_and_styles():
    html = "<div>Hello <b>Ajay</b>,</div><style>.a{}</style><p>Line&amp;two</p><script>evil()</script><ul><li>one</li><li>two</li></ul>"

    assert textutil.html_to_text(html) == "Hello Ajay,\n\nLine&two\n\none\n\ntwo"
    assert textutil.html_to_text("") == ""


def test_tidy_collapses_blank_lines_and_odd_spaces():
    assert textutil.tidy("a ​ b  \r\n\r\n\r\n\r\nc  ") == "a b\n\nc"


def test_cap():
    assert textutil.cap("abcdef", 4) == ("abcd", True)
    assert textutil.cap("abc", 4) == ("abc", False)


def test_embedded_images_are_removed_from_exported_markdown():
    markdown = "# Title\n\n![][image1]\n\nText\n\n[image1]: <data:image/png;base64,AAAA+/9=>\n" \
               "Inline ![x](data:image/jpeg;base64,QUJD) end"

    cleaned = textutil.strip_data_uris(markdown)

    assert "base64" not in cleaned
    assert "# Title" in cleaned and "Text" in cleaned and "end" in cleaned


# --- time ------------------------------------------------------------------


def test_shift_months_clamps_the_day():
    assert timeutil.shift_months(datetime(2026, 3, 31, tzinfo=timezone.utc), -1).date().isoformat() == "2026-02-28"
    assert timeutil.shift_months(datetime(2026, 11, 12, tzinfo=timezone.utc), -12).date().isoformat() == "2025-11-12"
    assert timeutil.shift_months(datetime(2026, 11, 30, tzinfo=timezone.utc), 3).date().isoformat() == "2027-02-28"


def test_rfc3339():
    assert timeutil.rfc3339(datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)) == "2026-01-02T03:04:05Z"
