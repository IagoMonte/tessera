#!/usr/bin/env bash
# Roda todos os testes: unitários do painel e do agente, integração dos scripts.
set -e
cd "$(dirname "$0")"
(cd painel && python3 -W ignore -m unittest 2>&1 | tail -4)
(cd agente && python3 -W ignore -m unittest 2>&1 | tail -4)
./testes/shell.sh
