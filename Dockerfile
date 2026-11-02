FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

RUN useradd --create-home --uid 10001 appuser \
    && chown appuser:appuser /app

COPY --chown=appuser:appuser . .

# Fail the backend image build if a neutral shared contract (shared/*.json,
# also read by the web app) is missing, malformed or no longer importable from
# the deployed /app layout. Each consumer validates its contract at import.
RUN test -f /app/shared/xp-levels.json \
    && test -f /app/shared/compliance-policy.json \
    && test -f /app/shared/stage2-policy.json \
    && test -f /app/shared/performance-focus-policy.json \
    && test -f /app/shared/training-calendar.json \
    && test -f /app/shared/api-messages.json \
    && test -f /app/shared/equipment-aliases.json \
    && test -f /app/shared/training-metadata.json \
    && test -f /app/shared/equipment-requirements.json \
    && test -f /app/shared/training-disciplines.json \
    && test -f /app/shared/training-focus-patterns.json \
    && python -c "from fightcamp.training_metadata import load_projection; from fightcamp.equipment_requirements import load_equipment_reviews; load_projection(); load_equipment_reviews()" \
    && python -c "from api.xp_levels import XP_LEVELS; assert XP_LEVELS[-1] == (8, 'Champion', 10000)" \
    && python -c "import api.compliance, api.performance_focus, api.contracts.training_day, api.contracts.training_disciplines, api.contracts.training_focus, api.errors, api.models" \
    && python -c "import fightcamp.session_sequencing, fightcamp.training_context; from fightcamp.stage2_policy import stage1_fallback_status; stage1_fallback_status()"

USER appuser

EXPOSE 8000

CMD ["uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
