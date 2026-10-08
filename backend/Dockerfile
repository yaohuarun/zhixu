FROM python:3.11-slim AS dependencies
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY pyproject.toml constraints.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    python -c "import subprocess,tomllib; subprocess.check_call(['pip','install','-c','constraints.txt',*tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']])"

FROM dependencies AS core
COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations
RUN pip install --no-deps --no-build-isolation .
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM dependencies AS worker
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources && \
    rm -f /etc/apt/apt.conf.d/docker-clean
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    apt-get -o Acquire::Retries=5 update && \
    apt-get -o Acquire::Retries=5 install -y --no-install-recommends libreoffice-writer fonts-noto-cjk libgl1 libglib2.0-0 libgomp1 && \
    rm -rf /var/lib/apt/lists/*
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-deps paddlepaddle==3.0.0 -i https://www.paddlepaddle.org.cn/packages/stable/cpu/
COPY ocr-constraints.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    sed -e 's/PyYAML==6.0.3/PyYAML==6.0.2/' -e 's/packaging==26.3/packaging==24.2/' constraints.txt > /tmp/ocr-constraints.txt && \
    python -c "import subprocess,tomllib; subprocess.check_call(['pip','install','-c','/tmp/ocr-constraints.txt','-c','ocr-constraints.txt',*tomllib.load(open('pyproject.toml','rb'))['project']['optional-dependencies']['ocr']])" && \
    pip check
COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations
RUN pip install --no-deps --no-build-isolation .
ENV PADDLE_PDX_CACHE_HOME=/data/models
CMD ["python", "-m", "app.worker"]
