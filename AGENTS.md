<claude-mem-context>
# Memory Context

# [afair] recent context, 2026-05-25 6:42pm GMT+2

Legend: 🎯session 🔴bugfix 🟣feature 🔄refactor ✅change 🔵discovery ⚖️decision 🚨security_alert 🔐security_note
Format: ID TIME TYPE TITLE
Fetch details: get_observations([IDs]) | Search: mem-search skill

Stats: 50 obs (17,969t read) | 447,601t work | 96% savings

### May 25, 2026
2198 4:51p ⚖️ Fly Deployment: GitHub Actions + Blue/Green Strategy Confirmed
2200 4:52p ⚖️ Fly Machine Lifecycle: Single Machine, Kill Old on Deploy
2201 5:03p 🟣 fly.toml Written: Single-Machine, Immediate Strategy, Always-On
2202 " ⚖️ Dockerfile Runs as Root — Documented Deviation from Non-Root Global Rule
2203 " 🟣 docs/operations.md Created: Full Fly Operations Runbook
2204 " 🟣 GitHub Actions Deploy Workflow Created
2205 5:04p 🔵 fly CLI Authenticated Locally; No FLY_API_TOKEN in Environment
2206 " 🔵 fly CLI Session Expired — Re-authentication Required
2207 5:08p ⚖️ Deploy Strategy Formally Accepted: strategy="immediate" for Phase 0
2208 5:23p 🔵 Claude Code MCP Server Auth Opens Browser for OAuth Login
2209 5:24p 🟣 afair: Fly.io Deploy + GitHub Actions CI Pipeline Shipped
2210 " 🔵 Clerk OAuth with Claude.ai MCP Has Known Bug; MCP Spec Requires OAuth 2.1
2211 5:30p 🔵 Clerk Cannot Act as OAuth Authorization Server for MCP Servers
2212 " 🔵 Clerk Has MCP OAuth Support via mcp-demo and mcp-tools Repos — Contradicts Docs
2213 " 🔵 clerk/mcp-tools Is Node.js Only — No Python Support for afair
2214 " 🔵 Claude.ai Clerk OAuth MCP Issue #164 Closed — Resolution Unknown, Claude Code CLI Works
2215 " ⚖️ Bearer Token Chosen as afair Phase 0 Auth Strategy
2216 5:31p 🔵 FastMCP Middleware API Shape for Bearer Token Auth Implementation
2217 5:37p 🟣 Bearer Token Auth Field Added to afair Settings
2218 5:38p 🟣 Production Boot Guard: Settings Fails if ENVIRONMENT=fly and AUTH_TOKEN Unset
2219 " 🟣 BearerTokenMiddleware Created as ASGI Middleware for afair MCP Server
2220 " 🟣 afair Server Refactored: ASGI Starlette Wrapper with Auth Middleware Added
2221 " 🟣 Comprehensive Auth Test Suite Created for afair Bearer Token Middleware
2222 5:39p ✅ AFAIR_AUTH_TOKEN Added to .env.example with Generation Instructions
2223 " 🔵 FastMCP ASGI Integration Requires lifespan=mcp_app.lifespan in Parent Starlette App
2224 " 🔴 Fixed FastMCP ASGI Integration: lifespan=mcp_app.lifespan Added to Starlette Wrapper
2225 5:40p 🟣 All Auth Tests Green — Bearer Token Implementation Complete and Verified
2226 " 🟣 AFAIR_AUTH_TOKEN Generated and Staged in Fly Secrets + .env.local
2227 " 🚨 AFAIR_AUTH_TOKEN Marked Compromised — Leaked via .env.local Diff in Chat Transcript
2228 " 🔵 CI Deploy Failure Root Cause: Fly Remote Builder Cannot Find Dockerfile in GitHub Actions
2229 " 🟣 afair Redeployed to Production with Bearer Token Auth Active
2230 5:42p 🔵 Production /health Returns 503 After Auth Middleware Deploy — Database Check Failing
2231 " 🔵 Production Boot Guard Working — Server Crash-Looping Because Staged Secret Not Activated
2232 6:05p 🟣 Automated MCP client installer script created
2233 6:12p 🔴 install_clients.py crashed on empty JSON config files
2234 " ✅ docs/clients/README.md updated with one-command installer section
S609 User asked if afair MCP is already wired into the current Claude Code session — it is not yet (May 25 at 6:13 PM)
S611 Diagnosing why afair MCP isn't appearing in session despite installer success — found two-file Claude config split (May 25 at 6:14 PM)
2235 6:15p 🔵 All MCP clients were already configured before installer ran
S612 Fixed installer to write to ~/.claude.json — afair now in active Claude Code config, ready for session restart (May 25 at 6:15 PM)
S610 Installer run confirmed all MCP clients already configured — session restart is the only remaining step (May 25 at 6:15 PM)
2236 6:16p 🔵 Claude Code reads MCP config from ~/.claude.json, not ~/.claude/settings.json
2237 " 🔴 install_clients.py fixed to write to ~/.claude.json (the actual Claude Code config)
S613 Phase 0 capability gate achieved — first live afair remember call from Claude Code succeeded end-to-end (May 25 at 6:17 PM)
2238 6:17p 🔵 afair MCP tools now available in Claude Code session
2239 " 🟣 Phase 0 capability gate achieved — first live afair remember call from Claude Code
S614 Task #6 marked completed — Phase 0 cross-vendor MCP verification done, 6 of 7 tasks complete (May 25 at 6:17 PM)
2240 6:18p 🟣 Full remember→recall round-trip verified end-to-end in Claude Code
S615 User opened the Phase 0 journal and attempted Claude.ai connector — blocked by OAuth requirement (May 25 at 6:19 PM)
2241 6:22p ✅ Task #7 started — Phase 0 two-week daily-use journal
2242 6:23p 🟣 Phase 0 daily-use journal created with Day 1 entry
S616 Inspecting Codex CLI config to verify afair MCP entry and auth format (May 25 at 6:24 PM)
2243 6:26p 🔵 afair MCP Server Returns No Resources or Templates
2244 6:27p 🔵 Codex CLI uses http_headers not headers — afair entry may be missing auth
2245 " 🔵 Codex afair TOML block uses headers not http_headers — format mismatch confirmed
S617 Phase 0 complete verification: all three client instruction files confirmed, CI green, production healthy — session winding down to daily-use phase (May 25 at 6:27 PM)
2246 6:29p 🔵 Codex MCP Server Configuration
2247 " 🔵 afair recall depth='normal' Not Yet Implemented (Phase 0)
2248 6:36p ⚖️ OAuth layer prioritized as next phase — bearer-only auth deemed insufficient
S618 User directed: finish Task #7 (daily-use journal window) then implement OAuth ASAP to stop relying solely on bearer token auth (May 25 at 6:37 PM)
**Investigated**: - CI run 26410481597 (dda7010) confirmed green — all 16 steps passed including Deploy and Verify health (duplicate observation, already documented)
    - All three client instruction files confirmed present (duplicate observation, already documented)
    - Primary session re-presented the full client matrix table and snippet content to the user before pivoting to OAuth

**Learned**: - The user explicitly pulled OAuth forward from Phase 1+ to immediate priority — "ASAP" and "not only rely on bearer"
    - OAuth 2.1 + PKCE is the only path to Claude.ai connector support (Claude.ai UI only accepts Client ID + Client Secret, not custom Authorization header)
    - Task #7 (14-day journal window) is the last in-progress task; completing it means marking it done and beginning the OAuth implementation work
    - The single shared bearer token is a known security concern (leaked into chat transcripts during the session); OAuth would enable per-user tokens with proper revocation

**Completed**: - Phase 0 build: 6/7 tasks done, CI green, production healthy at dda7010
    - All three automatable clients fully configured (transport config + model-layer instruction snippet)
    - 14-day journal window open (Task #7 in_progress), Day 1 fully documented
    - Vault: 3 events from two vendors (Claude Code + Codex CLI) — I5 cross-vendor invariant proven

**Next Steps**: - Immediately: close Task #7 (mark daily-use journal window as complete or transition it)
    - Then: design and implement OAuth 2.1 + PKCE layer on the afair MCP server
    - OAuth implementation scope: authorization server (or Clerk integration), token endpoint, PKCE flow, Claude.ai connector registration
    - This enables Claude.ai as the third client, multi-user support, and eliminates the single leaked bearer token as the sole auth mechanism


Access 448k tokens of past work via get_observations([IDs]) or mem-search skill.
</claude-mem-context>