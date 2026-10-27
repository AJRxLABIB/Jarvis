# Installs the AI models Jarvis needs into Ollama, then builds the local "jarvis" model from them.
# Run from D:\Jarvis:   .\scripts\setup_models.ps1
# ("jarvis" is NOT downloadable: it is made on your PC from qwen3:4b-instruct. `ollama pull jarvis` always fails.)
$ErrorActionPreference = "Stop"
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) { throw "Ollama isn't installed or not on PATH (https://ollama.com/download)." }
try { ollama list | Out-Null } catch { Start-Process ollama -ArgumentList "serve" -WindowStyle Hidden; Start-Sleep 5 }

ollama pull qwen3:4b-instruct          # the chat model "jarvis" is built from (about 2.5 GB, fits a 6 GB GPU)
ollama pull nomic-embed-text           # search by meaning (EMBEDDING_MODEL)
ollama pull qwen2.5vl:3b               # looking at the screen (VISION_MODEL) - optional, about 3 GB
if (Test-Path ".\.venv\Scripts\Activate.ps1") { . .\.venv\Scripts\Activate.ps1 }
python run.py --make-jarvis-model      # creates "jarvis" from qwen3:4b-instruct and switches Jarvis to it
ollama list
