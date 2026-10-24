#!/bin/bash
# ============================================================
# Script de despliegue automático para PythonAnywhere
# Ejecutar en la Bash Console de PythonAnywhere:
#   bash <(curl -sL https://raw.githubusercontent.com/xuxillo/gfantasy-backend/main/setup_pythonanywhere.sh)
# ============================================================

echo "🚀 Instalando dependencias del backend Fantasy..."
pip install --upgrade pip 2>&1 | tail -1
pip install -r ~/gfantasy-backend/requirements.txt 2>&1 | tail -5

echo ""
echo "📝 Creando archivo .env..."
if [ ! -f ~/gfantasy-backend/.env ]; then
    cat > ~/gfantasy-backend/.env << 'ENVEOF'
# JWT Secret (cámbialo a un valor seguro de 32+ caracteres)
JWT_SECRET=gurutze-fantasy-prod-secret-cambiar-en-produccion-2024

# CORS: permitir el frontend de Vercel
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
echo "🗄️  Inicializando base de datos Fantasy..."
cd ~/gfantasy-backend
python -c "from fantasy_db import init_db; init_db(); print('✅ BD inicializada')" 2>&1

echo ""
echo "📋 Resumen de verificación:"
echo "  - Repo: https://github.com/xuxillo/gfantasy-backend"
echo "  - DB path: ~/gfantasy-backend/data/sofascore.db"
echo "  - WSGI: wsgi_pyanywhere:application"
echo ""
echo "✅ Listo! Ve a la pestaña 'Web' en PythonAnywhere y haz clic en 'Reload'"
echo "   o visita: https://xuxillo.pythonanywhere.com"