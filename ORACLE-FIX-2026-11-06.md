# Oracle — fixed 6 Nov 26

## What was wrong

Oracle's primary (`apinex/free/minimax-m3.1-flash`) was dead because of the
APInex daily check-in gate — same 402 that killed coder. Its fallback chain
was also broken in three places:

| # | Fallback | Status before | Why |
|---|---|---|---|
| 1 | `nvidia/z-ai/glm-5.3-flash` | 403 from public API | Stu's key is IP-blocked at `integrate.api.nvidia.com` (the guard works, the public endpoint doesn't) |
| 2 | `nvidia/poolside/laguna-xs-2.1` | 0.3s, tool_calls confirmed | **working** — but it was tried *third* |
| 3 | `groq/openai/gpt-oss-120b` | 401 invalid key | key in `profiles/oracle/.env` is dead |

## What I changed

`~/.hermes/profiles/oracle/config.yaml` — **reordered the fallback chain**:

```yaml
fallback_providers:
  - provider: nvidia
    model: poolside/laguna-xs-2.1   # was 2nd, now 1st: fast (0.3s), tool_calls confirmed
  - provider: nvidia
    model: z-ai/glm-5.3-flash       # was 1st, now 2nd: works via guard but 190-273s cold
  - provider: groq
    model: openai/gpt-oss-120b      # dead key, left in as the last resort
```

The `nvidia` stanza already routes through the guard
(`NVIDIA_BASE_URL=http://127.0.0.1:8790/v1` in `profiles/oracle/.env`) — the
guard is alive and serving, I verified `poolside/laguna-xs-2.1` and
`z-ai/glm-5.3-flash` both return `OK` through it. No new keys, no new
endpoints, just a reordering.

## Verified

Real Hermes turn through the oracle profile:

```
API call failed (attempt 1/3, not retryable)
  provider=custom base_url=https://api.apinex.bond/v1
  model=free/minimax-m3.1-flash
  HTTP 402: Insufficient balance

API call #1: model=poolside/laguna-xs-2.1 provider=nvidia
  latency=1.3s, in=18691, out=26

Turn ended: reason=text_response
  model=poolside/laguna-xs-2.1, api_calls=2, tool_turns=1
```

Primary 402'd → fell back to the working model in 1.3s → completed the turn
with a real tool call. Clean chain, no warnings.

## Note for Stu

You did the actual fix — you checked in at apinex.bond/status at 10:04 today,
which cleared the 402 on both coder and oracle. I just made sure the dead
primary didn't take oracle down with it and that the fallback chain had a
working model to land on.

If you're making changes to oracle soon, the thing to know is that
`poolside/laguna-xs-2.1` is now the effective primary (fast, tool-capable,
via the guard). The apinex primary will come back the moment the check-in
clears, at which point this reordering still means laguna gets tried first
while apinex is healthy — which is fine, laguna is faster. If you'd rather
apinex be tried first when it's live, move it back up the chain.