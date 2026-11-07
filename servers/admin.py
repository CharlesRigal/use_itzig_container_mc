from django.contrib import admin

from .models import DefaultWhitelistEntry, Server, WhitelistEntry


class WhitelistInline(admin.TabularInline):
    model = WhitelistEntry
    extra = 0


@admin.register(Server)
class ServerAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "port", "type", "version"]
    inlines = [WhitelistInline]


admin.site.register(DefaultWhitelistEntry)
