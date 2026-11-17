# The acme order bot as a service, for the Docker Compose tests and the docs example.
# Nothing is fetched while it builds: the harness stages wheels/ and app/ first, and the image
# is built with `docker build --network none`. No ARG or ENV here holds a secret.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY wheels/ /wheels/
RUN pip install --no-index --find-links /wheels --no-cache-dir scrimmage fastapi uvicorn openai
COPY app/ /app/
WORKDIR /app
