# Security Policy

afair stores people's memory. The whole point is that the data is private and
durable, so security reports get taken seriously.

## Reporting a vulnerability

Please report security issues **privately**, not in a public issue or pull
request.

- Email **hello@afair.ai** with the subject line starting `[SECURITY]`, or
- Use GitHub's private vulnerability reporting (the "Report a vulnerability"
  button under the repository's Security tab).

Include enough to reproduce: what you did, what happened, and the impact you
think it has. A proof of concept helps but is not required.

You can expect an acknowledgement within a few days. afair is maintained by a
small team, so please allow reasonable time for a fix before any public
disclosure. Credit is given to reporters who want it.

## Scope

In scope: the MCP server, the substrate (storage + encryption), authentication
(bearer tokens and the OAuth flow), and anything that could let one party read,
write, or destroy another party's vault.

Out of scope: vulnerabilities in third-party dependencies (report those
upstream, though a heads-up is welcome), and issues that require physical access
to a machine the operator already controls.

## How the security model is meant to work

This is the design a report should hold afair against. A gap between this and
the code is exactly what's worth reporting.

- **At rest.** When a vault key is set, the SQLite database is opened through
  SQLCipher (whole-file AES-256) and filesystem blobs are written with
  AES-256-GCM. The key is required in production; the server refuses to boot
  without it (`ENVIRONMENT=fly`).
- **In transit.** The MCP surface is served over HTTPS in any real deployment.
- **Authentication.** Every request to `/mcp` carries either a static bearer
  token or an OAuth-issued JWT. The server refuses to start in production
  without an auth token configured.
- **Isolation.** afair is single-tenant by design (Invariant I8): one vault,
  one operator, no shared database. There is no multi-tenant query path to leak
  across.
- **Secrets.** No secrets live in the repository. They come from the
  environment and are validated at boot.
- **Scoped tokens.** Sensitive side-channels (signup, export) use their own
  narrowly-scoped bearers, so a leak of one does not grant full vault access.

## Supported versions

afair is pre-1.0. Security fixes land on `main` and ship in the next release.
Run a recent build.
