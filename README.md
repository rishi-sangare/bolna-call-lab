# Call Lab for Bolna

Why Bolna voice-agent calls drop at ~12 seconds, reproduced on the real open-source [Bolna](https://github.com/bolna-ai/bolna) engine and diagnosed from Bolna's own logs.

**Watch the 5-minute walkthrough:** https://rishi-sangare.github.io/bolna-call-lab/
**Plan and architecture:** https://rishi-sangare.github.io/bolna-call-lab/plan/

## What it does

- `engine/`: hosts the real Bolna `AssistantManager` over WebSocket, logging every engine line to JSONL.
- `lab/fake_twilio.py`: a caller that speaks Twilio's exact Media Streams protocol (`connected`, `start`, 20 ms mu-law `media`, `mark`, `clear`, `stop`), so to Bolna it is a normal phone call.
- `lab/scenarios.py`: five agents, each one way a call dies (or doesn't):

  | Scenario | What breaks | How Bolna ends it |
  |---|---|---|
  | Healthy call, caller pauses | nothing; caller is silent > 10 s | `inactivity_timeout` at 10–12 s |
  | Agent voice never plays | TTS voice ID not in the account | `inactivity_timeout` |
  | Speech-to-text rejected | STT provider returns HTTP 402 | `transcriber_connection_error` |
  | Caller audio never arrives | inbound media never reaches Bolna | `inactivity_timeout` |
  | Fixed configuration | silence 25 s, check-in at 8 s | caller hangs up normally |

- `doctor/analyze.py`: reads the engine log and the caller's events, checks each pipeline stage, and says which one broke, with evidence and the fix.
- `lab/ui/`: the Call Lab UI (call timeline, pipeline check, conversation, diagnosis, latency breakdown).

## Run it

Python 3.12. Bolna is vendored at `vendor/bolna` (v0.10.283, commit `ba03a96`); clone it there and `pip install -e vendor/bolna`.

```bash
cp .env.example .env   # ElevenLabs + Groq (or OpenAI) keys
python -m uvicorn engine.server:app --port 5001 --ws wsproto
python -m uvicorn lab.server:app --port 7860
```

Open http://localhost:7860, pick a scenario and place a call.
