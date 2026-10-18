"""Fine-tune Whisper on YOUR recordings (made with SPEECH_DATASET, exported with "export my Bangla recordings" or
``python run.py --export-speech-dataset bn``), then convert it for faster-whisper so Jarvis can use it.

This is OPTIONAL and runs outside Jarvis. It needs an NVIDIA GPU: 6 GB (e.g. RTX 4050 laptop) trains "small" (the default; settings are picked from your
GPU's memory); "medium" needs ~10 GB and "large-v3-turbo" ~16 GB
and these packages in a separate environment (they are large and NOT part of Jarvis):

    pip install "transformers>=4.40" datasets soundfile accelerate evaluate jiwer ctranslate2 torch

Usage:
    python scripts/finetune_whisper.py data/exports/speech_bn --base openai/whisper-small --language bn
then set in .env:   STT_BANGLA_MODEL=<the printed folder>

Honest limits: a few dozen clips will not make a big difference; a few hundred corrected clips of your voice start to.
The script keeps 10 % of the clips aside and prints the word error rate before and after, so you can see if it
helped - keep the old setting if it did not. Not run in Jarvis's own tests (no GPU there).
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path


def read_folder(folder: Path) -> list[tuple[str, str]]:
    with (folder / "metadata.csv").open(encoding="utf-8") as handle:
        rows = [(str(folder / r["file_name"]), r["transcription"]) for r in csv.DictReader(handle)]
    if len(rows) < 20:
        sys.exit(f"Only {len(rows)} clips - collect more first (at least 20; a few hundred is better).")
    return rows


def profile(vram_gb: float, base: str) -> dict:
    """Batch / memory settings that fit this GPU; refuses a model that can't train in it."""
    needs = 16 if "large" in base else 10 if "medium" in base else 5
    if vram_gb < needs:
        raise SystemExit(f"{base} needs about {needs} GB of VRAM to train; this GPU has {vram_gb:.0f} GB. "
                         "Use --base openai/whisper-small (it still helps as Jarvis's Bangla second opinion).")
    if vram_gb < 8:
        return {"batch": 2, "accumulate": 8, "checkpointing": True, "optim": "adafactor"}
    return {"batch": 8, "accumulate": 2, "checkpointing": False, "optim": "adamw_torch"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder", type=Path, help="an exported speech folder (clips/ + metadata.csv)")
    parser.add_argument("--base", default="openai/whisper-small", help="the Whisper model to start from")
    parser.add_argument("--language", default="bn")
    parser.add_argument("--epochs", type=float, default=5.0)
    parser.add_argument("--out", type=Path, default=None, help="where to put the result (default: next to the data)")
    args = parser.parse_args(argv)

    try:
        import evaluate
        import soundfile
        import torch
        from transformers import (Seq2SeqTrainer, Seq2SeqTrainingArguments, WhisperForConditionalGeneration,
                                  WhisperProcessor)
    except ImportError as exc:
        sys.exit(f"Missing package ({exc.name}). Install the packages listed at the top of this script.")
    if not torch.cuda.is_available():
        sys.exit("No CUDA GPU found: fine-tuning on the CPU would take days. Run this on a PC with an NVIDIA GPU.")
    free, total = (b / 2**30 for b in torch.cuda.mem_get_info())
    settings = profile(total, args.base)
    if free < total * 0.7:
        sys.exit(f"Only {free:.1f} GB of {total:.1f} GB VRAM is free: close Jarvis and stop Ollama's model first "
                 "(ollama ps / ollama stop <model>), then run this again.")

    rows = read_folder(args.folder)
    split = max(2, len(rows) // 10)
    test, train = rows[:split], rows[split:]
    processor = WhisperProcessor.from_pretrained(args.base, language=args.language, task="transcribe")
    model = WhisperForConditionalGeneration.from_pretrained(args.base)
    model.generation_config.language, model.generation_config.task = args.language, "transcribe"
    wer = evaluate.load("wer")

    def features(pairs):
        out = []
        for path, text in pairs:
            audio, rate = soundfile.read(path, dtype="float32")
            item = processor(audio=audio, sampling_rate=rate, text=text)
            out.append({"input_features": item["input_features"][0], "labels": item["labels"]})
        return out

    def collate(batch):
        inputs = processor.feature_extractor.pad([{"input_features": b["input_features"]} for b in batch],
                                                 return_tensors="pt")
        labels = processor.tokenizer.pad([{"input_ids": b["labels"]} for b in batch], return_tensors="pt")
        ids = labels["input_ids"].masked_fill(labels.attention_mask.ne(1), -100)
        if (ids[:, 0] == model.config.decoder_start_token_id).all():
            ids = ids[:, 1:]
        inputs["labels"] = ids
        return inputs

    def score(m) -> float:
        m.eval()
        said, truth = [], []
        for path, text in test:
            audio, rate = soundfile.read(path, dtype="float32")
            feats = processor(audio=audio, sampling_rate=rate, return_tensors="pt").input_features.to(m.device)
            with torch.no_grad():
                ids = m.generate(feats, max_new_tokens=120)
            said.append(processor.batch_decode(ids, skip_special_tokens=True)[0])
            truth.append(text)
        return wer.compute(predictions=said, references=truth)

    model.to("cuda")
    before = score(model)
    out = args.out or args.folder.parent / f"whisper-{args.language}-personal"
    training = Seq2SeqTrainingArguments(output_dir=str(out / "hf"), per_device_train_batch_size=settings["batch"],
                                        gradient_accumulation_steps=settings["accumulate"],
                                        gradient_checkpointing=settings["checkpointing"], optim=settings["optim"],
                                        learning_rate=1e-5, warmup_steps=20,
                                        num_train_epochs=args.epochs, fp16=True, save_strategy="no",
                                        logging_steps=10, report_to=[], remove_unused_columns=False)
    Seq2SeqTrainer(model=model, args=training, train_dataset=features(train), data_collator=collate).train()
    after = score(model)
    print(f"Word error rate on your held-back clips: before {before:.1%}, after {after:.1%}")
    if after >= before:
        print("It did not get better - keep your current setting (nothing was changed in Jarvis).")
        return 1
    hf = out / "hf"
    model.save_pretrained(hf)
    processor.save_pretrained(hf)
    import ctranslate2.converters

    ct2 = out / "ct2"
    ctranslate2.converters.TransformersConverter(str(hf), copy_files=["tokenizer.json", "preprocessor_config.json"]
                                                 ).convert(str(ct2), quantization="int8_float16", force=True)
    print(f"Done. To use it, add to .env:  STT_BANGLA_MODEL={ct2}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
