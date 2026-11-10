#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "=================================================="
echo "🎙️  Podcast Studio • Núcleo da Fé"
echo "=================================================="

# Check ffmpeg
if ! command -v ffmpeg &> /dev/null; then
    echo "⚠️  AVISO: 'ffmpeg' não foi encontrado no PATH."
    echo "   Instale com: brew install ffmpeg"
fi

# Ensure .venv exists
if [ ! -d ".venv" ]; then
    echo "⚙️  Criando ambiente virtual Python (.venv)..."
    python3 -m venv .venv
    echo "📦 Instalando dependências..."
    .venv/bin/pip install --upgrade pip -q
    .venv/bin/pip install -r requirements.txt -q
fi

# Ensure dependencies are installed
if ! .venv/bin/python3 -c "import boto3, yt_dlp, PIL" &> /dev/null; then
    echo "📦 Atualizando dependências de requirements.txt..."
    .venv/bin/pip install -r requirements.txt -q
fi

# Check .env
if [ ! -f ".env" ]; then
    echo "⚠️  AVISO: Arquivo .env não encontrado. Algumas funções do Cloudflare R2 podem falhar."
fi

PORT="${PORT:-8000}"
URL="http://localhost:$PORT"

# Se um Studio antigo ainda estiver rodando nesta porta, encerra para carregar o código atualizado
OLD_PIDS="$(lsof -ti tcp:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
if [ -n "$OLD_PIDS" ]; then
    for pid in $OLD_PIDS; do
        if ps -o command= -p "$pid" | grep -q "server.py"; then
            echo "♻️  Encerrando Studio antigo em execução (PID $pid) para carregar a versão atual..."
            kill "$pid" 2>/dev/null || true
        else
            echo "❌ A porta $PORT está em uso por outro programa (PID $pid). Use: PORT=8001 ./studio.sh"
            exit 1
        fi
    done
    sleep 1
fi

echo ""
echo "🚀 Iniciando o Podcast Studio em: $URL"
echo "   Pressione Ctrl+C para encerrar o servidor a qualquer momento."
echo ""

# Open browser in background after 1 second
(sleep 1 && open "$URL" 2>/dev/null || xdg-open "$URL" 2>/dev/null || true) &

# Run server with .venv python
exec .venv/bin/python3 server.py

