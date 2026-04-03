FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md /app/
COPY src /app/src
COPY alembic /app/alembic
COPY alembic.ini streamlit_app.py /app/
COPY sql /app/sql

RUN pip install --upgrade "pip>=25.0" "setuptools>=80.0.0" "wheel>=0.46.2" \
    && pip install ".[ui]"

RUN useradd --create-home --shell /bin/bash aigm \
    && mkdir -p /app/logs /app/component_state \
    && chown -R aigm:aigm /app

USER aigm

CMD ["python", "-m", "aigm.ops.db_api"]
