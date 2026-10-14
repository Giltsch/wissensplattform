# Log-Shipping zu Grafana Loki

Docker-Compose schreibt standardmäßig nach `json-file` lokal — bei
Container-Rebuild sind die Logs weg. Für Produktiv-Deployments lohnt es
sich, die Logs an einen externen Loki zu pushen. Das hier ist die
minimale Variante mit Grafana Cloud (Free-Tier deckt eine
Wissensplattform locker ab).

## 1. Loki-Docker-Driver installieren

Auf dem VPS:

```bash
docker plugin install grafana/loki-docker-driver:latest --alias loki --grant-all-permissions
```

Verfügbarkeit prüfen:

```bash
docker plugin ls | grep loki
```

## 2. Grafana-Cloud-Credentials

In Grafana Cloud → "My Account" → "Send Logs" — kopiere:

- `LOKI_URL` (z. B. `https://logs-prod-eu-west-0.grafana.net`)
- `LOKI_USER` (numerisch)
- `LOKI_API_KEY` (Token)

Ablage in `/etc/rag/loki.env` auf dem VPS, restriktiv geschützt:

```
LOKI_URL=https://logs-prod-eu-west-0.grafana.net
LOKI_USER=123456
LOKI_API_KEY=glc_eyJv…
```

```bash
sudo chmod 600 /etc/rag/loki.env
```

## 3. docker-compose.yml ergänzen

Pro Service einen `logging`-Block:

```yaml
services:
  backend:
    # ... bestehende Konfiguration ...
    logging:
      driver: loki
      options:
        loki-url: "${LOKI_URL}/loki/api/v1/push"
        loki-external-labels: "service=backend,env=prod"
        loki-batch-size: "400"
        # Auth über basic-auth-Header.
        loki-auth-username: "${LOKI_USER}"
        loki-auth-password: "${LOKI_API_KEY}"
```

Identisch für `postgres` und `frontend` (Service-Label entsprechend).

## 4. Stack neu starten

```bash
set -a; source /etc/rag/loki.env; set +a
docker compose up -d --force-recreate
```

`set -a` exportiert alle gelesenen Variablen automatisch, sodass die
`${LOKI_*}`-Substitution in `docker-compose.yml` greift.

## 5. In Grafana querien

LogQL-Beispiele:

```logql
# Alle Backend-Logs des letzten 30-Min-Fensters
{service="backend"}

# Nur fehlgeschlagene Logins
{service="backend"} |= "login_fail"

# 4xx-Antworten der letzten Stunde
{service="backend"} |= "HTTP" |~ " 4\\d\\d "

# Chat-Quota-Blocks
{service="backend"} |= "chat_quota_block"
```

## 6. Empfohlene Alerts

In Grafana Cloud → Alerting → einrichten:

- **Backend down**: `count_over_time({service="backend"}[5m]) == 0` über 5 Min
- **Spike fehlgeschlagener Logins**: `rate({service="backend"} |= "login_fail" [5m]) > 0.5`
- **Quota-Wall**: `count_over_time({service="backend"} |= "chat_quota_block" [1h]) > 50`
- **OpenRouter-Fehler**: `count_over_time({service="backend"} |= "openrouter" |= "ERROR" [10m]) > 5`

## Alternativen

- **Self-hosted Loki**: zusätzlicher Container, kein Cloud-Abhängigkeit, aber
  mehr Operations.
- **Vector**-Sidecar (`timberio/vector`): tail Docker-Logs lokal, ship
  nach beliebigem Backend (S3, ELK, Datadog). Leistungsfähiger, ein
  Drittanbieter mehr.
- **Promtail** (Loki-Agent): macht im Prinzip das, was der Docker-Loki-Driver
  schon abdeckt; nur sinnvoll wenn man auch System-Logs (`/var/log/syslog`)
  shippen will.
