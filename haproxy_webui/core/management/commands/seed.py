"""Seed initial admin superuser and sync/seed HAProxy backends."""
import os
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from core.haproxy_client import HAProxyError, list_configured_backends
from core.models import Backend


class Command(BaseCommand):
    help = "Create or update the initial admin user and seed/sync HAProxy backends."

    def handle(self, *args, **options):
        User = get_user_model()
        username = os.environ.get("ADMIN_USERNAME", "admin").strip() or "admin"
        email = os.environ.get("ADMIN_EMAIL", "admin@haproxy.local").strip()
        password = os.environ.get("ADMIN_PASSWORD", "admin123")

        user, created = User.objects.get_or_create(
            username=username,
            defaults={
                "email": email,
                "is_staff": True,
                "is_superuser": True,
            },
        )
        if not created:
            user.email = email
            user.is_staff = True
            user.is_superuser = True
        user.set_password(password)
        user.save()

        action = "Created" if created else "Updated"
        self.stdout.write(
            self.style.SUCCESS(f"✓ {action} admin superuser '{username}' ({email})")
        )

        # Try syncing configured backends directly from HAProxy Data Plane API
        try:
            configured = list_configured_backends()
            for item in configured:
                Backend.objects.update_or_create(
                    name=item["name"],
                    defaults={
                        "mode": item["mode"],
                        "balance_algorithm": item["algorithm"],
                    },
                )
            names = ", ".join(b["name"] for b in configured) or "(none)"
            self.stdout.write(
                self.style.SUCCESS(f"✓ Synced {len(configured)} backend(s) from HAProxy: {names}")
            )
        except HAProxyError as exc:
            self.stdout.write(
                self.style.WARNING(
                    f"! HAProxy Data Plane API not reachable ({exc.message}). Seeding default backends from haproxy.cfg..."
                )
            )
            defaults = [
                ("test-app_be", Backend.Mode.HTTP, Backend.BalanceAlgorithm.ROUNDROBIN),
            ]
            for name, mode, algo in defaults:
                Backend.objects.update_or_create(
                    name=name,
                    defaults={"mode": mode, "balance_algorithm": algo},
                )
            self.stdout.write(
                self.style.SUCCESS("✓ Seeded default backends: test-app_be")
            )
