# Model candidates to measure (researched 2026-11; nothing here is adopted)

Measure before switching: `powershell -ExecutionPolicy Bypass -File scripts\try_model.ps1 <model>` (downloads the model with Ollama, then runs the
model comparison, the memory check, the Bangla check and the screen-pointing trial; it changes no setting).

| Candidate | What it is | Checked | Open questions |
|---|---|---|---|
| `qwen3.5:4b` | Chat model with image input, tools and thinking; 3.3-4.0 GB | In the official Ollama library (ollama.com/library/qwen3.5), tags vision / tools / thinking | Speed and tool-call reliability on the RTX 4050; whether its pointing beats `qwen2.5vl:3b` (which missed 3 of 3 controls) |
| `phi4-mini` | Small chat model, reported good at structured output | Named by several 6 GB guides (blog-level sources) | Not checked in the Ollama library by me; Bangla quality unknown |
| ZonUI-3B | Model trained for pointing at screen controls; code public | Paper + GitHub (Han1018/ZonUI-3B) | Not in Ollama; needs a separate runtime; memory needed to run is not stated |
| KV-Ground-4B | Screen-pointing model, 67.0 on ScreenSpot-Pro (its own report) | Hugging Face model card | Same as above |
| BanglaSpeech2Text | Whisper fine-tuned for Bangla, offline | Project page | Batch, not live; whether it beats the current speech model on YOUR voice |

Rules kept: no paid service, nothing downloaded without you running the command, the 3D core and settings are untouched.
If `qwen3.5:4b` does well on both chat and pointing it can replace the chat model AND the vision model (one model loaded instead of two).
`REASONING_ROUTING=true` in `.env` lets the reasoning level pick the model role (level 3+ -> smart model); it is OFF until you have compared answers with it on and off.

## Embedding model (search by meaning)
Current: `nomic-embed-text` (English-focused). Candidate for Bangla + English: `qwen3-embedding:0.6b` (~639 MB, multilingual; vendor score 64.3 on multilingual MTEB).
To try it: `ollama pull qwen3-embedding:0.6b`, set `EMBEDDING_MODEL=qwen3-embedding:0.6b` in `.env`, then `python run.py --reembed` (re-embeds stored text; can be stopped and
continued; items not yet done still match by exact words). Measure FIRST, before switching anything: `python run.py --search-eval --model qwen3-embedding:0.6b` prints both models side by side (English, Bangla and cross-language). Switch only on a clear gap. To go back: restore the old value and run `--reembed` again.

## Measured on LABIB's PC, 2026-11-14: `qwen3.5:4b`
- Bangla answers: 12/15 fully right (94.9%), ~1.3 s per answer. Wrong on the national flower in both runs (said রুই, then আঙুর; it is শাপলা) and misspelt New Delhi once.
  No baseline was run in the same session, so "better than the current model" is NOT established.
- Memory answers: 8 of 10 - the same total as the earlier run with the current model (multi-session still missed).
- Screen pointing: 0 of 3 with and without zoom, ~32 s per answer, one timeout. Worse and far slower than `qwen2.5vl:3b`. Do NOT use it as the vision model.
- `--memory-eval` checks the memory store's rules, not search by meaning: it cannot measure an embedding-model change.
- Baseline, same day, current `jarvis:latest`: 11/15 fully right (93.2%), ~4.2 s per answer. So on Bangla answers `qwen3.5:4b` is about EQUAL in quality (one question apart = noise)
  and about 3x FASTER (1.4 s vs 4.2 s). Tool-calling quality is not yet compared: `python run.py --self-improve` measures that on real requests and only switches on a clear win.
