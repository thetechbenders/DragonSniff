"""HTTP opening policy shared by the fixed-endpoint device clients."""

from urllib.request import HTTPRedirectHandler, Request, build_opener


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Returning None makes urllib raise HTTPError for the original 3xx.
        # Never send a second request, even to another path on the same origin.
        return None


def open_device_request(request: Request, *, timeout: float):
    """Open one device request without following redirects or changing global policy."""
    return build_opener(_RejectRedirects()).open(request, timeout=timeout)
