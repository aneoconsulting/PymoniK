FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

ARG USE_PYTHON_VERSION="3.10.12"
# Set up working directory
WORKDIR /app

# Set up the ArmoniK user (required by ArmoniK)
RUN groupadd --gid 5000 armonikuser && \
    useradd --home-dir /home/armonikuser --create-home --uid 5000 --gid 5000 --shell /bin/sh --skel /dev/null armonikuser && \
    mkdir /cache && \
    chown armonikuser: /cache && \
    chown -R armonikuser: /app 
    

COPY --chown=armonikuser:armonikuser pymonik /pymonik 

USER armonikuser

    
RUN echo "Writing Python version: $USE_PYTHON_VERSION" && echo "$USE_PYTHON_VERSION" >> .python-version
RUN cat .python-version 

COPY pymonik_worker/pyproject.toml . 

RUN sed -i 's/source = "uv-dynamic-versioning"/source = "env"/' /pymonik/pyproject.toml
ENV PYMONIK_BUILD_VERSION="0.0.0"

RUN uv sync 

# Copy application code
COPY pymonik_worker/worker.py .

# activate uv venv
ENV PATH="/app/.venv/bin:$PATH" 
# Set environment for Python unbuffered output
ENV PYTHONUNBUFFERED=1


# Default command to start as a worker
ENTRYPOINT ["python", "worker.py"]
