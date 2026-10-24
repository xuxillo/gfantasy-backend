#!/bin/bash
# ============================================================
# Script de despliegue automático para PythonAnywhere
# -----------------------------------------------------------
# Descarga y ejecuta en la Bash Console de PythonAnywhere:
#   bash <(curl -sL https://raw.githubusercontent.com/xuxillo/gfantasy-backend/main/setup_pythonanywhere.sh)
# ============================================================

set -e

echo "🚀 [1/5] Instalando dependencias del backend Fantasy..."

# Detect Python version for the webapp (PythonAnywhere webapps use Python 3.11)
# Install for BOTH system Python 3.11 AND virtualenv (if exists)
VENV_DIR="/home/xuxillo/.virtualenvs/python-3.11"

# Primary: use python3.11 (system Python, includes user site-packages)
if command -v python3.11 &>/dev/null; then
    PIP_CMD="python3.11 -m pip"
elif [ -d "$VENV_DIR" ]; then
    PIP_CMD="$VENV_DIR/bin/pip"
else
    PIP_CMD="pip"
fi

echo "  Using: $PIP_CMD"
$PIP_CMD install --upgrade pip 2>&1 | tail -1
$PIP_CMD install -r ~/gfantasy-backend/requirements.txt 2>&1 | tail -5

# Also install in virtualenv if it exists (PythonAnywhere creates one automatically)
if [ -d "$VENV_DIR" ]; then
    echo "  Also installing in virtualenv at $VENV_DIR..."
    $VENV_DIR/bin/pip install -r ~/gfantasy-backend/requirements.txt 2>&1 | tail -5
fi

echo ""
echo "📝 [2/5] Creando archivo .env..."
if [ ! -f ~/gfantasy-backend/.env ]; then
    cat > ~/gfantasy-backend/.env << 'ENVEOF'
# JWT Secret (cámbialo a un valor seguro de 32+ caracteres)
JWT_SECRET=gurutze-fantasy-prod-secret-cambiar-en-produccion-2024

# CORS: permitir el frontend de Vercel
CORS_ORIGINS=https://gfweb-one.vercel.app,https://gfweb.vercel.app,http://localhost:3000

# Desactivar API interna de SofaScore (solo hay en local)
# En PythonAnywhere NO hay un server en 127.0.0.1:8000, así que se desactiva
# y el backend usa el CDN externo con curl_cffi como fallback.
SOFASCORE_API=

# Desactivar loops de fondo (usar endpoints bajo demanda)
GURUTZE_DISABLE_AUTO_CLOSE=1
GURUTZE_DISABLE_SOFA_UPDATE=1
GURUTZE_NIGHTLY_DISABLE=1
ENVEOF
    echo "✅ .env creado"
else
    echo "⚠️  .env ya existe, no se sobreescribe"
fi

echo ""
echo "🗄️  [3/5] Inicializando base de datos Fantasy..."
cd ~/gfantasy-backend
python -c "from fantasy_db import init_db; init_db(); print('✅ BD inicializada')" 2>&1

echo ""
echo "⚙️  [4/5] Configurando archivo WSGI..."
# Find the WSGI config file
WSGI_CONFIG="/home/xuxillo/.pythonanywhere/wsgi_xuxillo_pythonanywhere_com.py"
if [ -f "$WSGI_CONFIG" ]; then
    cat > "$WSGI_CONFIG" << 'WSGI'
import os
import sys

# --- Force clean module cache (prevents stale code on PythonAnywhere reload) ---
# PythonAnywhere's mod_wsgi may retain old module versions in sys.modules
# unless we explicitly delete them. This ensures the WSGI app always picks
# up the latest code from git pull.
for _mod in list(sys.modules):
    if _mod.startswith(('server_fantasy', 'wsgi_pyanywhere', 'fantasy_db',
                         'fantasy_', 'sofa_', 'password_reset', 'events')):
        del sys.modules[_mod]

# --- Add backend directory to Python path ---
# Ensure ~/gfantasy-backend has HIGHEST priority in sys.path
_home = os.path.expanduser("~")
_backend_dir = os.path.join(_home, "gfantasy-backend")
# Remove and re-insert at position 0 for guaranteed priority
for _d in [_backend_dir, os.path.join(_home, "backend")]:
    if _d in sys.path:
        sys.path.remove(_d)
if os.path.isdir(_backend_dir):
    sys.path.insert(0, _backend_dir)

# --- Environment variables ---
os.environ.setdefault("CORS_ORIGINS", "https://gfweb-one.vercel.app")
os.environ.setdefault("GURUTZE_DISABLE_AUTO_CLOSE", "1")
os.environ.setdefault("GURUTZE_DISABLE_SOFA_UPDATE", "1")
# Desactivar API interna de SofaScore (no hay server en 127.0.0.1:8000 en PA)
os.environ.setdefault("SOFASCORE_API", "")

# Import the WSGI application (this re-imports everything fresh)
from wsgi_pyanywhere import application
WSGI
    echo "✅ Archivo WSGI configurado en: $WSGI_CONFIG"
else
    echo "⚠️  No se encontro el archivo WSGI en $WSGI_CONFIG"
    echo "   Crea/edita el archivo WSGI manualmente desde la pestana 'Web'"
fi

echo ""
echo "[EXTRA] Verificando base de datos de SofaScore..."
if [ -f ~/gfantasy-backend/data/sofascore.db ]; then
    DB_SIZE=$(du -h ~/gfantasy-backend/data/sofascore.db | cut -f1)
    echo "  Base de datos encontrada: data/sofascore.db ($DB_SIZE)"
    echo "  Jugadores en BD: $(python -c 'import sqlite3; c=sqlite3.connect("~/gfantasy-backend/data/sofascore.db"); print(c.execute("SELECT COUNT(*) FROM jugadores").fetchone()[0])' 2>/dev/null || echo 'N/A')"
else
    echo "  ADVERTENCIA: data/sofascore.db no encontrada"
    echo "  Necesitas subirla desde tu máquina local con:"
    echo "    scp -r data/ xuxillo@xuxillo.pythonanywhere.com:~/gfantasy-backend/"
    echo "  También sube las imágenes:"
    echo "    scp -r output/ xuxillo@xuxillo.pythonanywhere.com:~/gfantasy-backend/"
fi

echo ""
echo "📋 [5/5] Resumen de verificacion:"
echo "  - Repo: https://github.com/xuxillo/gfantasy-backend"
echo "  - Directorio: ~/gfantasy-backend/"
echo "  - BD: ~/gfantasy-backend/data/sofascore.db"
echo "  - WSGI: wsgi_pyanywhere:application"
echo "  - SOFASCORE_API: desactivada (empty env var) en PythonAnywhere"
echo "  - CORS: configurada en .env"
echo ""
echo "Si las imágenes/fotos no cargan, sube también:"
echo "  scp -r output/ xuxillo@xuxillo.pythonanywhere.com:~/gfantasy-backend/"
echo ""
echo "Para verificar jornada de SofaScore:"
echo "  curl https://xuxillo.pythonanywhere.com/admin/sofascore-info"
echo ""
echo "Para forzar una jornada (si la BD está desactualizada):"
echo "  curl -X POST https://xuxillo.pythonanywhere.com/leagues/1/admin/matchday \\"
echo "    -d '{\"matchday\": 10}'"
echo ""
echo "✅ ¡Listo! Ve a la pestana 'Web' en PythonAnywhere y haz clic en 'Reload'."
echo "   Luego visita: https://xuxillo.pythonanywhere.com"