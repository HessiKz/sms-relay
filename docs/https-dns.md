# HTTPS and DNS

The relay is served from an existing domain on this host. Nothing new is
invented and no new public port is opened.

## Current setup

- Caddy (v2.x) is the only public entry point and already owns ports 80/443.
- The relay backend binds `127.0.0.1:8090` and is reachable only from
  Caddy on the same machine.
- The site block for the relay domain gains a path-scoped handler:

```caddyfile
ai-barrow.ir {
    reverse_proxy localhost:3000

    handle /relay/* {
        uri strip_prefix /relay
        reverse_proxy 127.0.0.1:8090
    }
}
```

`handle` blocks are mutually exclusive and are matched before the
site-level `reverse_proxy`, so the existing web app keeps working exactly
as before. `uri strip_prefix /relay` removes the prefix, so the backend
receives plain paths (`/health`, `/sms`, `/messages`, ...).

The API base URL used by the Android app is therefore
`https://ai-barrow.ir/relay`.

## DNS

```bash
dig +short ai-barrow.ir      # must resolve to this VPS' public IP
```

If it points somewhere else, fix the A record at your DNS provider first
(Caddy will not issue a certificate for a name that does not resolve to
this host). The Caddyfile must keep the same hostname that the record
points to — certificates are issued per name.

## TLS

Caddy obtains and renews certificates automatically (ACME/Let's Encrypt) for
the names in the Caddyfile. Nothing extra is required, but:

- port 80 and 443 must be reachable from the internet for the HTTP
  challenge,
- the certificate is stored in Caddy's data directory
  (`/var/lib/caddy` or the configured storage path); do not delete it or
  the next reload will re-issue certificates,
- if the domain uses Cloudflare proxying ("orange cloud"), the ACME
  HTTP-01 challenge may need to be handled by Cloudflare instead
  (switch to DNS-01 or set the record to DNS-only while issuing).

## Applying and verifying

```bash
caddy validate --config /etc/caddy/Caddyfile   # always first
caddy reload  --config /etc/caddy/Caddyfile    # graceful, keeps serving
```

Verification:

```bash
curl -i https://ai-barrow.ir/relay/health                 # 200 {"ok":true}
curl -i https://ai-barrow.ir/relay/sms -X POST \          # 401 without a token
     -H 'Content-Type: application/json' -d '{}'
curl -i https://ai-barrow.ir/healthz                      # whatever the web app does
```

A `502` from Caddy means the backend is not running or is not listening on
`127.0.0.1:8090` — check `systemctl status sms-relay-api` and
`ss -tlpn | grep 8090`.

## Firewall

No change is needed: Caddy already owns 80/443, the backend is
loopback-only, and no new port is opened. The relay never listens on
`0.0.0.0`. Check what the firewall currently allows before touching it:

```bash
ufw status verbose      # if ufw is installed
nft list ruleset | head -40
```

The only rule that should exist for the relay is Caddy → 127.0.0.1:8090,
which is not a firewall rule at all.
