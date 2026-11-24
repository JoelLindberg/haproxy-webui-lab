"""Database models for HAProxy Web UI."""
from django.core.validators import RegexValidator
from django.db import models

haproxy_name_validator = RegexValidator(
    regex=r"^[A-Za-z0-9_.:-]+$",
    message="Names may only contain letters, numbers, hyphens, underscores, dots, and colons.",
)


class Backend(models.Model):
    """Tracked HAProxy backend managed through the Web UI."""

    class Mode(models.TextChoices):
        HTTP = "http", "HTTP"
        TCP = "tcp", "TCP"

    class BalanceAlgorithm(models.TextChoices):
        ROUNDROBIN = "roundrobin", "Round Robin"
        LEASTCONN = "leastconn", "Least Connections"
        SOURCE = "source", "Source IP"
        FIRST = "first", "First Available"
        RANDOM = "random", "Random"

    name = models.CharField(
        max_length=255,
        unique=True,
        validators=[haproxy_name_validator],
    )
    mode = models.CharField(
        max_length=16,
        choices=Mode.choices,
        default=Mode.HTTP,
    )
    balance_algorithm = models.CharField(
        max_length=32,
        choices=BalanceAlgorithm.choices,
        default=BalanceAlgorithm.ROUNDROBIN,
    )
    description = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "haproxy_backends"
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.mode})"
