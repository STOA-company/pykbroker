"""Shared urllib boundary: broker credentials must never follow a redirect."""

from urllib.request import HTTPRedirectHandler, Request, build_opener


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Returning None lets urllib raise HTTPError through its existing error handler.
        return None


_OPENER = build_opener(_NoRedirect())


def urlopen(request: Request, *, timeout: float):
    """Open once; 3xx raises HTTPError for the adapter's existing error mapping."""
    return _OPENER.open(request, timeout=timeout)
