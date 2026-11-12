import os
import sys

from django.apps import AppConfig


class ServersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "servers"
    verbose_name = "Serveurs Minecraft"

    def ready(self):
        # Uniquement dans le service web (uvicorn), pas pour manage.py migrate/shell/tests.
        if os.path.basename(sys.argv[0]) in ("uvicorn", "gunicorn") or os.environ.get("MCM_SCHEDULER") == "1":
            from . import scheduler

            scheduler.start()
