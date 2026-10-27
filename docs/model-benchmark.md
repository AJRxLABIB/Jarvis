# Measured model comparison (round 49)

Run on your PC with Ollama running:

    python run.py --benchmark-models --priority balanced     # or speed | quality | low_resource
    python run.py --eval-quality --model <name>              # quality, per model (already existed; needed for a recommendation)

Per installed chat model it unloads the model, then runs a short, a two-sentence and a long-prompt request and records, from
Ollama's own timing fields: load time (cold), prompt speed, generation speed (median tokens/s; answers under 4 tokens are
ignored), first-response time (load + prompt, derived - not a streaming measurement), GPU share from `/api/ps` (1.0 = entirely on
the GPU; below 0.95 = partly on the CPU, which answers several times slower) and the GPU memory peak when `nvidia-smi` answers.
Results go to `data\benchmark\speed-history.jsonl` with the environment. Nothing is downloaded or changed; you apply a choice in Settings.

## Recommendation rules (`app/ai/modelbench.py::recommend`, tested)
* A model with no `--eval-quality` record is shown but never recommended. Each failed safety question costs 5 points in the ranking and is always printed (a hard ban would empty the list: on a real 6 GB PC every installed model failed at least one prompt-injection question). Models measured as only partly on the GPU are left out of speed / balanced / low_resource picks.
* GPU peak is the model's own rise over what was already in use before it loaded.
* quality: highest score. speed: fastest measured among models within 10 points of the best quality. low_resource: smallest footprint
  (measured GPU peak, else the file size as an estimate) within 10 points. balanced: 50 % quality, 30 % speed, 20 % size, missing
  components dropped and said so.
* On a small GPU, a model whose size suggests it will not fit is flagged "(estimate)"; once measured, the flag uses the real GPU share.
* Unmeasured values print as `-`, never 0.

## Limits
Not yet run against a real Ollama/GPU. Speed varies with context length, power mode and other programs on the GPU. Quality comes only
from the reference set and your own requests; the personal test set (`growth.cases`, `self_improve`) needs 8+ of your real requests
with known right tools. Fine-tuning (`scripts/finetune_llm.py`) was not run here.
