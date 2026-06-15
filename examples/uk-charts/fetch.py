## @file http.py
##
## @brief HTTP fetch utilities for the uk-charts demo.
##
## Provides two functions for fetching remote content:
##
##   ``HTTPFetch(url)``
##     Returns the decoded response body as a string.
##     Retries up to four times on HTTP 429 with exponential back-off.
##
##   ``HTTPFetchSoup(url)``
##     Returns a ``BeautifulSoup`` parse tree of the response.
##
## ``headers`` is a module-level dict applied to every request.  It is
## defined here and never changes at runtime.
##
## @copyright Copyright (c) 2026 Tim Hosking

from __future__ import annotations

import time
import urllib.error
import urllib.request
from bs4 import BeautifulSoup

## HTTP request headers sent with every fetch.
headers: dict[str, str] = {"User-Agent": "uk-charts-explorer/1.0"}


def HTTPFetch(url: str) -> str:
    """Fetch *url* and return the decoded body.  Retries up to four times on 429."""
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < 3:
                time.sleep(2 ** (attempt + 1))
                continue
            raise
    raise RuntimeError("HTTPFetch loop exited unexpectedly")


def HTTPFetchSoup(url: str):
    """Fetch *url* and return a BeautifulSoup parse tree."""
    return BeautifulSoup(HTTPFetch(url), "html.parser")
