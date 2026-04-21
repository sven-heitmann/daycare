FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y supervisor && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY logger.py json_utils.py utils.py caldav_sync.py notifier.py receiver.py cleanup.py cleanup_runner.py ./
COPY supervisord.conf /etc/supervisor/conf.d/daycare.conf
COPY entrypoint.sh /entrypoint.sh

# Dedizierter User ohne Login-Shell
RUN groupadd --gid 1500 daycare && \
    useradd --uid 1500 --gid 1500 --no-create-home --shell /sbin/nologin daycare && \
    # /app gehört daycare
    chown -R daycare:daycare /app && \
    chmod +x /entrypoint.sh && \
    # tty-Gruppe für Zugriff auf /dev/fd/* (supervisord log output)
    usermod -aG tty daycare

ENV CONFIG_PATH=/config/config.yml
ENV STATE_PATH=/data/state.json

# Entrypoint läuft kurz als Root (Secrets kopieren), wechselt dann zu daycare
ENTRYPOINT ["/entrypoint.sh"]
