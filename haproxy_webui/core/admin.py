from django.contrib import admin
from .models import Backend


@admin.register(Backend)
class BackendAdmin(admin.ModelAdmin):
    list_display = ("name", "mode", "balance_algorithm", "created_at", "updated_at")
    list_filter = ("mode", "balance_algorithm")
    search_fields = ("name", "description")
    readonly_fields = ("created_at", "updated_at")
