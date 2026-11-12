# Device HTTP redirect security

## Reproduction

Baseline: `main` at `807ef735278832b017d51b105cc8710172c832b4`.
On CPython 3.11.18, both production clients used `urllib.request.urlopen`
with its default redirect handler. An isolated test used two HTTP servers
bound to `127.0.0.1` on different ephemeral ports (different origins), with
no real devices or credentials. The first server returned an absolute
`Location` pointing at the second server's `/redirect-target` route.

For each of 301, 302, 303, 307, and 308:

- PrusaLink's fixed `GET /api/v1/status` generated a second-origin GET
  containing the synthetic `X-Api-Key: localhost-regression-key` header.
- All three Dragon JSON endpoints and the SSE endpoint followed the redirect
  and sent their custom Accept, Cache-Control, and User-Agent headers.
  Dragon currently sends no authentication header.
- Same-origin redirects also generated additional requests outside the fixed
  endpoints. These are rejected by the remediation as well.

The new regression tests, run before changing production code, produced 50
failing redirect subcases; the nonredirected control passed:

```console
PYTHONPATH=src python -W error -m unittest tests.test_http_redirects -v
```

## Root cause and implementation

The clients validate the initial origin and use fixed endpoint paths, but the
default urllib redirect handler constructs another request from `Location`.
In the tested Python 3.11 implementation, `redirect_request` copies request
headers other than Content-Length and Content-Type without checking whether
the origin changed. That includes PrusaLink's API key.

Both clients now default to a shared, local opener with a redirect handler
that returns no redirect request. urllib raises `HTTPError` with the original
3xx response; the existing bounded HTTP-error recording paths handle it.
No global urllib opener is installed, and no authentication format changes.
Constructor-injected openers remain available for tests and trusted callers;
a caller supplying its own opener is responsible for its redirect policy.

## Evidence and compatibility

- Dragon JSON retains the original status, timing, response headers, bounded
  body, and parse evidence as `http_response`, with `http_ok` and `ok` false.
- Dragon SSE retains the corresponding evidence as `sse_unavailable`, followed
  by `sse_closed`. No redirected stream opens.
- PrusaLink retains the original `response_status`, timing, reachability, and
  `http_status` error using its existing `source_observation` contract. It does
  not record arbitrary response headers or bodies, preserving credential
  redaction. A redirect is not an authenticated or fresh sample.
- Nonredirected requests, endpoint validation, response-size limits,
  connection budgets, and recording schemas are unchanged.
- Deployments relying on redirects, including same-origin canonical-path
  redirects, must configure the final device origin serving the fixed API
  endpoints directly. Redirects are deliberately no longer followed.

## Validation boundaries

The loopback tests cover all five statuses for both clients, all Dragon JSON
endpoints, SSE, same-origin and cross-origin destinations, original response
evidence, and nonredirected success. They assert zero target requests, rather
than merely checking that a credential was removed. PrusaLink's synthetic key
also stays out of recorded evidence and snapshots when echoed in the body.

No hardware or live-device acceptance is claimed. The reproduction uses HTTP
on loopback, not a separate HTTPS/TLS or DNS-rebinding integration fixture.
The change rejects redirects rather than trying to validate their targets.

An extra Python 3.14.4 focused run found the existing
`test_json_deeper_than_decoder_limit_is_contained` assertion failure: the
decoder accepts nesting that reaches DragonSniff's own depth validator,
producing a different error message. The same failure was independently
reproduced from a temporary archive of untouched baseline main. It is outside
this redirect change; CI uses Python 3.11.
