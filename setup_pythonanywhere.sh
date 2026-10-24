#!/bin/bash
# ============================================================
# Script de despliegue automático para PythonAnywhere
# -----------------------------------------------------------
# Descarga y ejecuta en la Bash Console de PythonAnywhere:
#   bash <(curl -sL https://raw.githubusercontent.com/xuxillo/gfantasy-backend/main/setup_pythonanywhere.sh)
# ============================================================

set -e

echo "🚀 [1/5] Instalando dependencias del backend Fantasy..."
pip install --upgrade pip 2>&1 | tail -1
pip install -r ~/gfantasy-backend/requirements.txt 2>&1 | tail -3

echo ""
echo "📝 [2/5] Creando archivo .env..."
if [ ! -f ~/gfantasy-backend/.env ]; then
    cat > ~/gfantasy-backend/.env << 'ENVEOF'
# JWT Secret (cámbialo a un valor seguro de 32+ caracteres)
JWT_SECRET=gurutze-fantasy-prod-secret-cambiar-en-produccion-2024

# CORS: permitir el frontend de Vercel
CORS_ORIGENS=https://gfweb-one.vercel.app,https://gfweb.vercel.app,http://localhost:3000
CORS_ORIGINS=https://gfweb-one.vercel.app,https://gfweb.vercel.app,http://localhost:3000

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

# Add the backend directory to Python path (code was imported from Git)
BACKEND_DIR = '/home/xuxillo/gfantasy-backend'
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

# Environment variables
os.environ.setdefault("CORS_ORIGENS", "https://gfweb-one.vercel.app")
os.environ.setdefault("GURUTZE_DISABLE_AUTO_CLOSE", "1")
os.environ.setdefault("GURUTZE_DISABLE_SOFA_UPDATE", "1")

# Import the WSGI application from our module
from wsgi_pyanywhere import application
WSGI
    echo "✅ Archivo WSGI configurado en: $WSGI_CONFIG"
else
    echo "⚠️  No se encontro el archivo WSGI en $WSGI_CONFIG"
    echo "   Crea/edita el archivo WSGI manualmente desde la pestana 'Web'"
fi

echo ""
echo "📋 [5/5] Resumen de verificacion:"
echo "  - Repo: https://github.com/xuxillo/gfantasy-backend"
echo "  - Directorio: ~/gfantasy-backend/"
echo "  - BD: ~/gfantasy-backend/data/sofascore.db"
echo "  - WSGI: wsgi_pyanywhere:application"
echo ""
echo "✅ ¡Listo! Ve a la pestana 'Web' en PythonAnywhere y haz clic en 'Reload'."
echo "   Luego visita: https://xuxillo.pythonanywhere.com"