from django.urls import path

from . import views

urlpatterns = [
    path("", views.server_list, name="server_list"),
    path("new/", views.server_create, name="server_create"),
    path("status.json", views.status_json, name="status_json"),
    path("settings/", views.app_settings, name="app_settings"),
    path("whitelist/", views.default_whitelist, name="default_whitelist"),
    path("whitelist/<int:pk>/remove/", views.default_whitelist_remove, name="default_whitelist_remove"),
    path("s/<slug:slug>/", views.server_detail, name="server_detail"),
    path("s/<slug:slug>/action/<str:action>/", views.server_action, name="server_action"),
    path("s/<slug:slug>/delete/", views.server_delete, name="server_delete"),
    path("s/<slug:slug>/logs/", views.server_logs, name="server_logs"),
    path("s/<slug:slug>/command/", views.server_command, name="server_command"),
    path("s/<slug:slug>/files/", views.server_files, name="server_files"),
    path("s/<slug:slug>/files/edit/", views.file_edit, name="file_edit"),
    path("s/<slug:slug>/files/restore/", views.file_restore, name="file_restore"),
    path("s/<slug:slug>/whitelist/add/", views.whitelist_add, name="whitelist_add"),
    path("s/<slug:slug>/whitelist/<int:pk>/remove/", views.whitelist_remove, name="whitelist_remove"),
    path("s/<slug:slug>/whitelist/default/", views.default_whitelist_apply, name="default_whitelist_apply"),
    path("s/<slug:slug>/backups/new/", views.backup_create, name="backup_create"),
    path("s/<slug:slug>/backups/schedule/", views.backup_schedule, name="backup_schedule"),
    path("s/<slug:slug>/backups/<str:name>/", views.backup_download, name="backup_download"),
    path("s/<slug:slug>/backups/<str:name>/restore/", views.backup_restore, name="backup_restore"),
    path("s/<slug:slug>/backups/<str:name>/delete/", views.backup_delete, name="backup_delete"),
]
