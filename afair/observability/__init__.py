"""Observability helpers — Sentry init, future log shipping etc."""

from .sentry import init_sentry

__all__ = ["init_sentry"]
