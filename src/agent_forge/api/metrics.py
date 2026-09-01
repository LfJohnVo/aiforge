"""The Prometheus scrape endpoint.

Unauthenticated on purpose, and safe to be: metrics carry counts and durations labelled
by tenant, model and node -- never content, never identity beyond the tenant id. What it
does reveal is traffic shape per tenant, so in a deployment it belongs behind the same
network boundary as the rest of the cell, not on a public ingress. Compose keeps it on the
``observability`` network for exactly that reason.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from agent_forge.observability.metrics import get_metrics

router = APIRouter(tags=["metrics"])

# The plain text exposition format, not OpenMetrics: Prometheus accepts both and every
# scraper understands the former.
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    """Current values of every metric this process holds."""
    return Response(content=get_metrics().render(), media_type=CONTENT_TYPE)
