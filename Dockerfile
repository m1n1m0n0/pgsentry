# syntax=docker/dockerfile:1

# ---- build stage: install pgsentry and its dependencies into a virtualenv -------------
FROM python:3.12-slim-bookworm AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN /opt/venv/bin/pip install .

# ---- runtime stage: only the virtualenv, no compiler, no pip cache, no source ---------
FROM python:3.12-slim-bookworm
# A numeric UID, so Kubernetes' runAsNonRoot can verify it without reading /etc/passwd.
RUN useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin pgsentry
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
USER 10001:10001
EXPOSE 9188
ENTRYPOINT ["pgsentry"]
CMD ["serve"]
