# LAN HTTPS validation for evidence downloads (issue #26)

This record closes the "NEEDS EXTERNAL VALIDATION" gate on issue #26 with a Brave HTTP-versus-HTTPS A/B test isolating transport security as the deciding factor in the reported download warning.

## Environment

- Browser: Brave 1.95.104 (Chromium 153.0.8010.53)
- Platform: Windows 11
- Test harness: a standalone stdlib-only Python HTTP/HTTPS server serving a dummy `.jsonl` file with the same response headers as DragonSniff's real `/local/v1/session/export` endpoint (`Content-Type: application/x-ndjson; charset=utf-8`, `Content-Disposition: attachment; filename="dragonsniff-session.jsonl"`, `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, `Content-Security-Policy: default-src 'self'; connect-src 'self'`, fixed `Content-Length`, no chunked transfer framing)
- TLS: a `mkcert`-issued, locally trusted certificate covering the LAN IP, `localhost`, and `127.0.0.1`
- Each case served the download from a landing page (click-through link), not a direct navigation, so the browser's Shields toggle had a chance to apply before the download fired

## Test matrix and results

| # | Transport | Shields | Result |
|---|---|---|---|
| 1 | `http://localhost:8080/` | On | Clean — no warning |
| 2 | `http://<lan-ip>:8080/` | On | **"Insecure Download Blocked"** — a yellow warning-triangle badge appears on the download entry before the file is kept; clicking it turns the entry grey and shows the warning text beneath it; requires an explicit "Keep" action |
| 3 | `https://<lan-ip>:8443/` | On | Clean — no warning |
| 4 | `http://<lan-ip>:8080/` | Off | Same "Insecure Download Blocked" warning as case 2 |
| 5 | `https://<lan-ip>:8443/` | Off | Clean — no warning |

## Findings

- The warning is driven purely by **LAN address + plain HTTP**. Brave's Shields setting made no observable difference (cases 2 and 4 are identical).
- A trusted HTTPS connection to the same LAN address, same payload, same filename, and same headers removes the warning entirely, with Shields in either state (cases 3 and 5).
- Localhost HTTP was never warned, consistent with Chromium/Brave treating loopback as a trusted context regardless of scheme.
- No MIME type, filename, or response-header change is justified: the response is byte-for-byte the same across all five cases, and only the transport/address combination changes the outcome. This matches the issue's own response inspection.

## Supported topology

Of the three options the issue lists, this validation supports **Option 2: a locally trusted certificate for the LAN hostname/IP**, terminated by a component in front of DragonSniff's loopback-only listener, which forwards the original `Host` and `Origin` unmodified rather than rewriting them to `localhost`. DragonSniff's own Host/Origin validation and read-only target allowlist (`src/dragonsniff/server.py`) are unaffected and must stay exactly as they are — this is a transport-layer addition, not a change to the application's trust boundary.

This is the one narrowly supported trusted-LAN topology from this validation:

- Issue a certificate for the specific trusted-LAN hostname or IP with a tool the operator's own devices trust locally (for example `mkcert`, after running its `-install` step on each client that needs to trust it). Do not attempt public ACME issuance for an RFC 1918 address.
- Terminate TLS with that certificate in front of the existing loopback-bound DragonSniff service, preserving the original `Host`/`Origin` authority end to end.
- Do not introduce a proxy whose purpose is to present a falsified `Host` or `Origin` of `localhost` to DragonSniff.
- Do not publish this as an internet-facing recipe; it assumes a trusted LAN and locally distributed trust, not public CA-backed certificates.

Option 1 (reusing an existing trusted-LAN reverse proxy with ordinary TLS termination) is compatible with the same forwarding requirement but is out of scope for this record since no such proxy was available to validate against. Option 3 (native Python TLS) was not pursued; DragonSniff's server intentionally binds only to loopback addresses, and terminating TLS on a LAN-facing interface is better left to a purpose-built proxy than to the application server itself.

## Acceptance criteria

- [x] Exact Brave warning text, version, and platform captured.
- [x] Same evidence payload and filename tested over localhost HTTP, LAN HTTP, and trusted LAN HTTPS.
- [x] Response headers captured and confirmed identical for each path.
- [x] Confirmed HTTPS removes the warning without disabling Shields.
- [x] Host/Origin validation and the read-only target allowlist are unchanged.
- [x] One narrowly supported trusted-LAN TLS topology documented above.
