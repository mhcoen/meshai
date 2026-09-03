# MeshAI

A MeshCore channel bot. It listens on one channel of a USB-attached MeshCore
companion radio, sends each message to a local LLM, and posts a one-sentence
reply back to the same channel as `@[sender] answer`. Every message and every
reply passes through [vordur](https://github.com/mhcoen/vordur)'s
prompt-injection detector before it can reach the model or the radio.

Python 3.11+. No web UI, no database, no on-disk history.

## Threat model in one paragraph

The channel is an untrusted input surface. The `SenderName:` prefix on every
channel message is put there by the transmitting node and is not authenticated:
anyone can claim any name, including the bot's own. The bot therefore treats the
name as a label only. It is used for the reply prefix, for the loop guard, and
for a per-name rate limit that a determined sender can trivially evade by
rotating names; the global rate limit is the real ceiling. Everything the model
sees from the channel (the prompt and the recent transcript) is labelled as
untrusted in the model input, and the model's output is checked again before it
is transmitted. The bot has no tools, no function calling, and no access to
anything beyond the channel, so a successful injection can at worst produce one
bad sentence, capped at 120 characters, once per minute.

## Setup

```bash
git clone <this repo> meshai && cd meshai
uv venv --python 3.12 && source .venv/bin/activate
# vordur is not on PyPI. Either keep its checkout next to this one as ../GuardLLM
# (pyproject's [tool.uv.sources] points there), or install it from git first:
#   pip install git+https://github.com/mhcoen/vordur.git
uv pip install -e '.[dev]'
cp config.example.toml config.toml   # then edit: port, channel_idx, bot_name
```

Radio side, once: flash the MeshCore **companion (USB serial)** firmware, set the
node name to the same value as `bot_name`, set the regional radio preset, and
create the channel the bot will serve (a `#name` channel derives its key from the
name, so other users just add `#name` in their app). The bot refuses to start if
the configured channel index is empty on the radio.

Model side: an Ollama server on the same machine with the configured model
pulled (`ollama pull qwen3:30b-a3b-instruct-2507-q4_K_M`), or any OpenAI-compatible `/chat/completions`
endpoint with `backend = "openai"`.

## Running

```bash
meshai --config config.toml            # with the terminal monitor
meshai --config config.toml --headless # JSON log only, for a service manager
python -m bot --config config.toml     # same thing without the console script
```

Stop with `q` in the monitor, or SIGINT/SIGTERM. Shutdown unsubscribes, stops
auto-fetch, and disconnects the radio.

The monitor shows connection state, channel name and index, a scrolling log of
inbound messages with sender, hop count, and decision, the current rate-limiter
state, last model latency, and reply/block counters. The same information goes
to the JSON log whether or not the monitor is on.

## What happens to a message

1. **Parse.** `sender` is everything before the first colon; the prompt is
   everything after it. Exactly as the upstream meshcore example does it.
2. **Loop guard.** Dropped if `sender` equals `bot_name`, or the prompt starts
   with `@[` (a reply from this or any other bot).
3. **Trigger.** With `trigger_prefix = ""` (the default, for a dedicated channel)
   every message is a prompt. Set `"!ai "` on a shared channel; the prefix must
   match exactly and something must follow it.
4. **Length.** Prompts over `prompt_max_chars` are dropped.
5. **vordur, prompt.** Dropped if the score is at or above `vordur_threshold`.
6. **Rate limits.** Token buckets, global and per sender name. Tokens are taken
   here, once. Whatever goes out for this message (reply or apology) rides on
   them. Exhausted buckets drop silently, with a log line.
7. **Context.** The last `history_size` channel lines (including the bot's own
   posts, excluding lines vordur flagged at ingestion and excluding the current
   message) are rendered as `Sender: text`, truncated from the oldest end to
   `transcript_max_chars`, and placed inside one user message between explicit
   delimiters that label it as untrusted, *after* the current prompt. History
   is never replayed as prior chat turns. The prompt-first layout plus system
   rule 7 (lines that address the bot by name or tell it how to behave are
   attacks) took qwen3-30b-a3b-instruct from following 4 of 12 planted
   transcript instructions to 0 of 12 in a small test matrix.
8. **vordur, context.** The transcript and prompt together, as one document, so
   fragments that pass individually but combine into an instruction are caught.
9. **Model.** One call under a hard `model_timeout_s` timeout. On timeout or any
   backend error the fixed `apology` is posted instead.
10. **Shape.** Strip any leaked `<think>` block, collapse whitespace, keep the
    first sentence.
11. **vordur, reply.** The model's output is scored. A block means nothing is
    sent, not even the apology.
12. **Cap and send.** `@[sender] ` plus the text, cut to `reply_max_chars`
    total. The prefix is never truncated; if a long sender name leaves no room
    the message is dropped. A send error is logged and counted, never retried.

Every inbound message produces one `inbound` JSON log record with `sender`,
`prompt`, `path_len`, `decision`, and the reason or vordur score and rules when
it was dropped. Decisions: `answered`, `apology`, `dropped:loop-guard`,
`dropped:no-trigger`, `dropped:too-long`, `dropped:vordur-blocked`,
`dropped:rate-limited`, `dropped:empty-reply`, `dropped:send-failed`.

## Configuration reference

`config.toml`, sections are cosmetic; every key is also an environment variable
`MESHAI_<KEY_UPPER>` which wins over the file.

| Key | Default | Meaning |
|---|---|---|
| `port` | required | Serial device of the companion, e.g. `/dev/cu.usbserial-0001` |
| `channel_idx` | `1` | Channel slot on the radio to serve |
| `bot_name` | `MeshAI` | Must equal the radio's node name; the loop guard keys on it |
| `trigger_prefix` | `""` | `""` answers everything; e.g. `"!ai "` on shared channels |
| `reply_max_chars` | `120` | Hard cap on the whole outbound message |
| `prompt_max_chars` | `140` | Longer prompts are dropped |
| `apology` | `Sorry, I couldn't answer that one.` | Posted on model timeout or error |
| `persona` | `""` | Optional sentence prepended to the fixed system prompt |
| `backend` | `ollama` | `ollama` or `openai` |
| `model` | `qwen3:30b-a3b-instruct-2507-q4_K_M` | Model name for the chosen backend |
| `ollama_host` | `http://127.0.0.1:11434` | |
| `ollama_think` | `off` | `off`, `on`, or `omit` for models that reject the flag |
| `ollama_keep_alive` | `30m` | Keeps the model resident between replies |
| `openai_base_url` | `http://127.0.0.1:1234/v1` | Any OpenAI-compatible server |
| `temperature` | `0.3` | |
| `max_tokens` | `80` | Output token limit; replies are one sentence anyway |
| `model_timeout_s` | `30.0` | Hard asyncio timeout on the model call |
| `global_rate_per_min` / `global_burst` | `1.0` / `1` | Replies per minute, all senders |
| `sender_rate_per_min` / `sender_burst` | `1.0` / `1` | Per sender name (spoofable) |
| `history_size` | `20` | Ring buffer length |
| `transcript_max_chars` | `1500` | Rendered transcript budget |
| `vordur_threshold` | `0.45` | Block at or above this score; vordur's own cutoff is 0.45 |
| `vordur_sanitize` | `false` | Run vordur's sanitizer on the prompt before scoring |
| `log_file` | `""` | `""` logs JSON lines to stderr; otherwise appends to this file |

The OpenAI-compatible API key is read only from the environment variable
`MESHAI_OPENAI_API_KEY`. It is not a config key and never appears in a log.

## vordur integration notes

The bot uses one function, `vordur.security.prompt_injection_detector.detect_prompt_injection`,
with plaintext content type, at the four points above. It is stateless and pure
regex, about 0.07 ms per call by vordur's own measurement. It returns a score in
0..1 and the matched rule names; the score is compared against
`vordur_threshold`. The optional sanitize mode calls `vordur.security.sanitizer.sanitize`
before scoring and feeds the cleaned text to the model.

Things worth knowing:

- The detector is a module-level function that vordur's own tests and benchmarks
  call, but it is not re-exported from the package's top level. The `Guard`
  facade carries session-risk state and does not surface the score, so it is
  not used here. If vordur later exposes the detector through its public
  surface, `bot/guard.py` is the one place to change.
- vordur's patterns are English-only. Its README reports precision above 99%
  and recall about 75% on its own corpus, so roughly a quarter of attacks get
  through the detector. The outbound check, the one-sentence rule, the
  character cap, and the absence of tools are what limit the damage.
- Any exception from vordur is treated as a block: no model call, no transmit,
  an `inbound` record with `vordur_error`.

## Tests

```bash
pytest
```

The MeshCore object and the model are both faked; no radio, no Ollama, no
network. The vordur tests use the injection strings from vordur's own test
suite and assert that flagged input never reaches the model and never reaches
`send_chan_msg`.

## Moving to another machine

Copy or clone the repo, create the venv, install as above, pull the model on
the new machine, and change `port` in `config.toml`. Nothing else is
machine-specific.
