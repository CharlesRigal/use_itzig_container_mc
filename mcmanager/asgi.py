"""Point d'entrée ASGI (uvicorn) : nécessaire pour les flux en direct (Server-Sent Events)."""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mcmanager.settings")

application = get_asgi_application()
