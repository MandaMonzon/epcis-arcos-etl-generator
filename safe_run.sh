#!/bin/bash
set -e
MAX_DRUGS=$1
MAX_EVENTS=$2
MAX_MEM_MB=${3:-6000}
 
if [ -z "$MAX_DRUGS" ] || [ -z "$MAX_EVENTS" ]; then
  echo "Uso: ./safe_run.sh <max_drugs> <max_events> [max_mem_mb]"
  exit 1
fi
 
MAX_MEM_KB=$((MAX_MEM_MB * 1024))
 
echo "=========================================="
echo "Gerando: $MAX_DRUGS medicamentos, $MAX_EVENTS eventos"
echo "Limite de memoria (ulimit -v): ${MAX_MEM_MB}MB"
echo "=========================================="
 
(
  ulimit -v "$MAX_MEM_KB"
  python3 main.py --lines 500000 --max-drugs "$MAX_DRUGS" --max-events "$MAX_EVENTS"
)
STATUS=$?
 
if [ $STATUS -ne 0 ]; then
  echo "ERRO: processo falhou ou excedeu o limite de memoria (status $STATUS)."
  exit $STATUS
fi
 
echo "OK: geracao concluida com sucesso dentro do limite de memoria."
