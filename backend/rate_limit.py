"""Shared slowapi Limiter.

Lives in its own module so route handlers in ``main.py`` AND in routers
imported from ``main`` (``totp.py``, ``admin.py``, ...) can use the same
``@limiter.limit("...")`` decorator without an import cycle.

Key function: ``slowapi.util.get_remote_address`` returns
``request.client.host``. With the backend behind nginx in the docker
network, that's the nginx container IP — which would collapse all clients
into a single bucket. The Dockerfile starts uvicorn with
``--forwarded-allow-ips '*'`` so uvicorn rewrites ``request.client``
from the ``X-Forwarded-For`` header that nginx sets. If you ever change
the topology, audit that envelope: a missing ``forwarded-allow-ips`` is
a silent global rate-limit bug (audit finding H-1, 2026-05-10).
"""
from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
