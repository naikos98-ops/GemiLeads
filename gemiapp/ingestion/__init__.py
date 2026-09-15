"""ΓΕΜΗ Open Data API access. Every request goes through gemiapp.ingestion.client.GemiClient."""

from .client import GemiClient, current_gemi_lane, gemi_lane, get_gemi_client, parse_error_body
from .errors import (
    GemiApiError,
    GemiAuthenticationError,
    GemiBadRequestError,
    GemiBudgetTimeoutError,
    GemiBudgetUnavailableError,
    GemiConfigurationError,
    GemiNotFoundError,
    GemiResponseFormatError,
    GemiRetryExhaustedError,
)
from .rate_budget import BudgetConfig, CacheBudgetStore, GemiLane, GemiRateBudget

__all__ = [
    "BudgetConfig",
    "CacheBudgetStore",
    "GemiApiError",
    "GemiAuthenticationError",
    "GemiBadRequestError",
    "GemiBudgetTimeoutError",
    "GemiBudgetUnavailableError",
    "GemiClient",
    "GemiConfigurationError",
    "GemiLane",
    "GemiNotFoundError",
    "GemiRateBudget",
    "GemiResponseFormatError",
    "GemiRetryExhaustedError",
    "current_gemi_lane",
    "gemi_lane",
    "get_gemi_client",
    "parse_error_body",
]
