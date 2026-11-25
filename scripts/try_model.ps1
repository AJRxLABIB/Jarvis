# Try a candidate chat model against your current one. Nothing is switched: it only measures.
#   powershell -ExecutionPolicy Bypass -File scripts\try_model.ps1 qwen3.5:4b
# qwen3.5:4b is in the official Ollama library (3.3-4.0 GB, text + image, tools, thinking) - checked 2026-11.
param([Parameter(Mandatory = $true)][string]$Model)

$ErrorActionPreference = "Stop"
Write-Host "1/4  Downloading $Model with Ollama (this is the only step that uses the internet)..."
ollama pull $Model
if ($LASTEXITCODE -ne 0) { throw "ollama pull failed" }

Write-Host "2/4  Speed and quality next to your current models..."
python run.py --compare-models $Model

Write-Host "3/4  Memory answers: your CURRENT model first (the baseline), then the candidate (10 questions; one question = 10 points)..."
python run.py --longmem-eval --full
$env:AI_MODEL = $Model
python run.py --longmem-eval --full
Remove-Item Env:AI_MODEL

Write-Host "Extra  Bangla answers: your CURRENT model first (the baseline), then the candidate..."
python run.py --eval-bangla
python run.py --eval-bangla --model $Model

Write-Host "4/4  Screen pointing. Switch to Notepad or Settings when asked."
python run.py --ground-trial $Model

Write-Host ""
Write-Host "Done. Nothing was changed. To use it, set AI_MODEL (and VISION_MODEL if pointing was good) in .env and run: python run.py --restart"
Write-Host "To remove it again: ollama rm $Model"
