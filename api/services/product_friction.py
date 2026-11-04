"""Count observed API failures without copying existing error logs or payloads."""

import logging
import uuid
from starlette.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)


def install_product_friction(app):
    @app.middleware("http")
    async def observed_failure(request, call_next):
        response = await call_next(request)
        actor = getattr(request.state, "athlete_id", None)
        path = request.url.path
        # Only authenticated failed feature operations; no auth, admin, reads,
        # analytics, or expected ownership/not-found responses.
        if (
            actor
            and response.status_code in (400, 409, 422, 500, 502, 503, 504)
            and request.method in ("POST", "PUT", "PATCH")
        ):
            feature = next(
                (
                    feature
                    for prefix, feature in (
                        ("/api/today/session-completion", "strength"),
                        ("/api/training/sessions", "manual"),
                        ("/api/onboarding/", "onboarding"),
                        ("/api/nutrition/", "nutrition"),
                        ("/api/today/checkin", "recovery"),
                        ("/api/today/sparring", "sparring"),
                        ("/api/custom-plans", "plans"),
                        ("/api/feedback/session", "feedback"),
                        ("/api/plans/generate", "plans"),
                    )
                    if path.startswith(prefix)
                ),
                None,
            )
            if feature:

                def record():
                    try:
                        store = request.app.state.store
                        store.client.rpc(
                            "ingest_product_event",
                            {
                                "p_athlete_id": actor,
                                "p_event_name": "operation_failed",
                                "p_feature": feature,
                                "p_request_id": str(
                                    uuid.uuid5(
                                        uuid.NAMESPACE_URL,
                                        str(
                                            getattr(
                                                request.state,
                                                "request_id",
                                                uuid.uuid4(),
                                            )
                                        ),
                                    )
                                ),
                            },
                        ).execute()
                    except Exception:
                        # Existing monitoring owns technical detail; telemetry must not break the response.
                        logger.debug(
                            "Product friction measurement unavailable"
                        )

                await run_in_threadpool(record)
        return response
