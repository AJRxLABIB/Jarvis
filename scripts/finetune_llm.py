"""Teach Jarvis's own AI from YOUR use: a LoRA fine-tune on the requests that went right (and the answers you liked),
turned into an Ollama model "jarvis-tuned". Jarvis then TESTS it on your real requests ("tune your AI") and switches
to it only if it scores better - so a bad training run can't make Jarvis worse.

OPTIONAL, runs outside Jarvis, needs an NVIDIA GPU (6 GB for 4B models such as Jarvis's qwen3:4b-instruct base,
8 GB+ for 7-8B; the settings are picked from your GPU's memory) and, in a separate
Python environment (large packages, NOT part of Jarvis):

    pip install unsloth          # brings torch, transformers, trl, peft, bitsandbytes

Steps:
    1. python run.py --export-training                 (writes data/exports/training.jsonl, redacted)
    2. python scripts/finetune_llm.py data/exports/training.jsonl --hf-base unsloth/Qwen3-4B-Instruct-2507 \
           --ollama-base qwen3:4b-instruct
       (--hf-base must be the SAME model family/size as your Ollama base, so the chat/tool template matches)
    3. In Jarvis: "tune your AI" (or python run.py --self-improve) - it compares jarvis-tuned with your current set-up.

Honest limits: below ~200 good examples a LoRA rarely helps; it can make the model worse at things you never asked
for, which is why the switch is decided by Jarvis's measured test, not by this script. Not run in Jarvis's own tests.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import subprocess
import sys
from pathlib import Path

MIN_EXAMPLES = 50
FREE_NEEDED_GB = 4.5          # what a 4B QLoRA needs free; close Jarvis / stop Ollama's model first


def profile(vram_gb: float, base: str) -> dict:
    """Training settings that fit this GPU. 6 GB (e.g. RTX 4050 laptop): 4B models in 4-bit, short sequences,
    batch 1 with gradient accumulation and Unsloth's gradient checkpointing. 7-8B needs about 8 GB or more."""
    big = any(tag in base.lower() for tag in ("7b", "8b", "9b", "12b", "14b"))
    if vram_gb < 8 and big:
        raise SystemExit(f"{base} is too big to train on {vram_gb:.0f} GB of VRAM. Use a 4B model (your Jarvis "
                         "base qwen3:4b-instruct -> unsloth/Qwen3-4B-Instruct-2507) or a GPU with 8 GB+.")
    if vram_gb < 8:
        return {"max_seq_length": 2048, "batch": 1, "accumulate": 8}
    if vram_gb < 16:
        return {"max_seq_length": 4096, "batch": 2, "accumulate": 4}
    return {"max_seq_length": 4096, "batch": 4, "accumulate": 2}


def load(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("messages"), list):
            rows.append(row)
    return rows


def modelfile_for(gguf: Path, base_modelfile: str) -> str:
    """The base model's Modelfile (template, stop words, parameters) with FROM pointing at the tuned weights, so
    tool calling works exactly like the base."""
    lines = [f"FROM {gguf.resolve()}"]
    for line in base_modelfile.splitlines():
        if line.strip().upper().startswith(("FROM ", "ADAPTER ")) or line.startswith("#"):
            continue
        lines.append(line)
    return "\n".join(lines).strip() + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("data", type=Path, help="training.jsonl from python run.py --export-training")
    parser.add_argument("--hf-base", required=True, help="Hugging Face model matching your Ollama base")
    parser.add_argument("--ollama-base", required=True, help="the installed Ollama model it corresponds to")
    parser.add_argument("--name", default="jarvis-tuned", help="the Ollama model to create")
    parser.add_argument("--epochs", type=float, default=2.0)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--quant", default="q4_k_m")
    args = parser.parse_args(argv)

    rows = load(args.data)
    if len(rows) < MIN_EXAMPLES:
        sys.exit(f"Only {len(rows)} examples - keep using Jarvis and export again later (at least {MIN_EXAMPLES}).")
    if shutil.which("ollama") is None:
        sys.exit("Ollama isn't on PATH - it's needed to create the tuned model.")
    try:
        import torch
        from datasets import Dataset
        from trl import SFTConfig, SFTTrainer
        from unsloth import FastLanguageModel
    except ImportError as exc:
        sys.exit(f"Missing package ({exc.name}). See the install line at the top of this script.")
    if not torch.cuda.is_available():
        sys.exit("No CUDA GPU found: this needs an NVIDIA GPU.")
    free, total = (b / 2**30 for b in torch.cuda.mem_get_info())
    settings = profile(total, args.hf_base)
    if free < FREE_NEEDED_GB:
        sys.exit(f"Only {free:.1f} GB of the GPU's {total:.1f} GB is free. Close Jarvis and run 'ollama stop "
                 f"{args.ollama_base}' (Ollama and speech recognition use the GPU), then try again.")
    print(f"GPU: {total:.1f} GB -> sequence {settings['max_seq_length']}, batch {settings['batch']} x "
          f"{settings['accumulate']}")

    base_file = subprocess.run(["ollama", "show", "--modelfile", args.ollama_base], capture_output=True, text=True,
                               shell=False)
    if base_file.returncode != 0:
        sys.exit(f"Ollama doesn't know {args.ollama_base}: {base_file.stderr.strip()}")

    model, tokenizer = FastLanguageModel.from_pretrained(args.hf_base, max_seq_length=settings["max_seq_length"],
                                                         load_in_4bit=True)
    model = FastLanguageModel.get_peft_model(model, r=16, lora_alpha=16, lora_dropout=0.0,
                                             use_gradient_checkpointing="unsloth",
                                             target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                                                             "gate_proj", "up_proj", "down_proj"])

    def render(row: dict) -> str:
        return tokenizer.apply_chat_template(row["messages"], tools=row.get("tools"), tokenize=False)

    random.Random(7).shuffle(rows)
    texts = [render(r) for r in rows]
    out = args.out or args.data.parent / args.name
    trainer = SFTTrainer(model=model, tokenizer=tokenizer, train_dataset=Dataset.from_dict({"text": texts}),
                         args=SFTConfig(output_dir=str(out / "train"), per_device_train_batch_size=settings["batch"],
                                        gradient_accumulation_steps=settings["accumulate"], learning_rate=1e-4,
                                        num_train_epochs=args.epochs, max_seq_length=settings["max_seq_length"],
                                        logging_steps=10, save_strategy="no", report_to=[], dataset_text_field="text"))
    trainer.train()
    model.save_pretrained_gguf(str(out / "gguf"), tokenizer, quantization_method=args.quant)
    ggufs = sorted((out / "gguf").glob("*.gguf"))
    if not ggufs:
        sys.exit("No GGUF file was written - check the unsloth output above.")
    modelfile = out / "Modelfile"
    modelfile.write_text(modelfile_for(ggufs[-1], base_file.stdout), encoding="utf-8")
    created = subprocess.run(["ollama", "create", args.name, "-f", str(modelfile)], shell=False)
    if created.returncode != 0:
        sys.exit("ollama create failed (see above).")
    print(f"Created Ollama model '{args.name}'. Now say 'tune your AI' in Jarvis: it tests it on your real requests "
          "and switches only if it does better.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
