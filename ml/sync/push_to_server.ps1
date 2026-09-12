# Push the ml/ code + configs + exported transcripts to the lab server (ssh alias "Mustree" in ~/.ssh/config).
# Usage: powershell -ExecutionPolicy Bypass -File ml\sync\push_to_server.ps1 [-Remote "Mustree"] [-Dest "~/deutsch-shorts"]
param([string]$Remote = 'Mustree', [string]$Dest = '~/deutsch-shorts')
$ErrorActionPreference = 'Stop'
$root = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
Set-Location $root
ssh $Remote "mkdir -p $Dest/ml/data/work $Dest/app $Dest/pipeline/prompts $Dest/data"
# code needed on the server: ml/ (without data/runs), app/models.py + taxonomy + config (imported by ml/common.py), prompt, taxonomy data
scp -r ml/*.py ml/configs ml/serve ml/requirements-ml.txt ml/README.md "${Remote}:$Dest/ml/"
scp app/__init__.py app/models.py app/taxonomy.py app/config.py "${Remote}:$Dest/app/"
scp pipeline/prompts/enrich_v1.md "${Remote}:$Dest/pipeline/prompts/"
scp data/topics.yaml data/de_50k.txt "${Remote}:$Dest/data/"
if (Test-Path ml\data\work\transcripts.jsonl) { scp ml\data\work\transcripts.jsonl "${Remote}:$Dest/ml/data/work/" }
if (Test-Path ml\data\gold) { scp -r ml\data\gold "${Remote}:$Dest/ml/data/" }
if (Test-Path ml\data\feedback) { scp -r ml\data\feedback "${Remote}:$Dest/ml/data/" }
Write-Host "pushed to ${Remote}:$Dest"
