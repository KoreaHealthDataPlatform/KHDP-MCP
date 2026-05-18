# KHDP context for Gemini CLI

Gemini CLI loads `GEMINI.md` at the workspace root for in-conversation
context (analogous to `CLAUDE.md` / `AGENTS.md`).

## Available KHDP tools (via MCP)

| Tool | What it does |
| --- | --- |
| `khdp_auth_status`  | Check whether the user is authenticated. |
| `khdp_auth_refresh` | Rotate the refresh token to extend the session. |
| `khdp_auth_logout`  | Delete locally cached tokens. |
| `khdp_api_request`  | Authenticated HTTP passthrough to the KHDP API. |

KHDP uses OAuth 2.0 + PKCE (RFC 7636) with a loopback redirect. Login
requires a browser session on the user's machine, so there is no MCP
login tool by design. If status reports `authenticated=false`, ask the
user to run `khdp login` in their terminal -- it opens their browser
to KHDP's login page.

## Workflow

1. Always begin a KHDP-touching task with `khdp_auth_status`.
2. If unauthenticated, ask the user to run `khdp login` themselves.
3. If `is_expired=true` and `has_refresh_token=true`, call
   `khdp_auth_refresh` before data calls.
4. Prefer dedicated KHDP MCP tools (when present) over
   `khdp_api_request`.
5. Never echo bearer tokens or PHI back to the user.
