"""ASGI entry point para PythonAnywhere.

Expon la aplicación FastAPI (`app` en server_fantasy.py) como
`application`, que es lo que PythonAnywhere busca al crear una
web app con "Manual configuration (ASGI)".
"""
from server_fantasy import app

# PythonAnywhere busca una variable llamada `application`.
application = app

# también exponemos `app` por si el entorno la necesita.
app = app  # noqa: F811 – re-asignación intencional