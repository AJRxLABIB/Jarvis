"""The optional fine-tune pipelines: training export carries the offered tools, a tuned model is tried first by the
measured self-improvement, and the scripts' pure parts (Modelfile, data checks) work. The training itself needs a GPU
and large packages and is not run here."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent import self_improve
from app.agent.growth import Growth
from app.memory.database import Database

ROOT = Path(__file__).resolve().parents[1]


def _script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_export_includes_the_tools_that_were_offered(tmp_path):
    from app.tools.catalog import build_default_registry

    db = Database(tmp_path / "x.db")
    db.initialize()
    g = Growth(db)
    ok = SimpleNamespace(call=SimpleNamespace(name="get_weather", arguments={}), result=SimpleNamespace(ok=True, error=None))
    g.record("what's the weather today", [ok], "jarvis")
    with db.connect() as conn:
        conn.execute("UPDATE learning_turns SET outcome = 'good'")
    path = tmp_path / "t.jsonl"
    assert self_improve.export_training(db, path, registry=build_default_registry()) == 1
    row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    names = {t["function"]["name"] for t in row["tools"]}
    assert "get_weather" in names and row["messages"][1]["tool_calls"][0]["function"]["name"] == "get_weather"


def test_tuned_model_is_tried_first():
    models = [SimpleNamespace(name=n, can_chat=True, supports_tools=True, supports_vision=False, size_bytes=s)
              for n, s in (("big:70b", 9e9), ("mid:14b", 5e9), ("other:7b", 4e9), ("jarvis-tuned:latest", 5e9))]
    options = self_improve.candidates("qwen3:8b", 0.2, models, total_ram=None)
    assert options[0].label == "current" and options[2].base == "jarvis-tuned:latest"
    assert options[2].label == "tuned on your use"


def test_llm_script_builds_the_modelfile_from_the_base(tmp_path):
    llm = _script("finetune_llm")
    base = "# comment\nFROM /blobs/sha256-abc\nTEMPLATE \"{{ .Prompt }}\"\nPARAMETER stop <|im_end|>\nADAPTER /x\n"
    text = llm.modelfile_for(tmp_path / "m.gguf", base)
    assert text.startswith(f"FROM {(tmp_path / 'm.gguf').resolve()}") and "TEMPLATE" in text
    assert "ADAPTER" not in text and "/blobs/" not in text
    data = tmp_path / "d.jsonl"
    data.write_text('{"messages": []}\nnot json\n', encoding="utf-8")
    assert len(llm.load(data)) == 1
    with pytest.raises(SystemExit):
        llm.main([str(data), "--hf-base", "x", "--ollama-base", "y"])          # too few examples: refuses


def test_whisper_script_refuses_tiny_datasets(tmp_path):
    whisper = _script("finetune_whisper")
    (tmp_path / "metadata.csv").write_text("file_name,transcription\nclips/a.wav,hi\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        whisper.read_folder(tmp_path)


def test_six_gb_gpu_profiles():
    llm, whisper = _script("finetune_llm"), _script("finetune_whisper")
    assert llm.profile(6.0, "unsloth/Qwen3-4B-Instruct-2507") == {"max_seq_length": 2048, "batch": 1, "accumulate": 8}
    with pytest.raises(SystemExit):
        llm.profile(6.0, "unsloth/Qwen3-8B")                      # an 8B model can't train in 6 GB
    assert llm.profile(12.0, "unsloth/Qwen3-8B")["batch"] == 2
    assert whisper.profile(6.0, "openai/whisper-small")["checkpointing"] is True
    with pytest.raises(SystemExit):
        whisper.profile(6.0, "openai/whisper-large-v3-turbo")
