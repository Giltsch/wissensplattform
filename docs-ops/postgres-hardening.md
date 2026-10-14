# Postgres-Hardening

Audit-Findings C3-MED-4 und C3-MED-5: Heute läuft die App mit dem
gleichen User wie die Migrations (Superuser des `POSTGRES_DB`), und die
Verbindung Backend↔Postgres ist nicht TLS-verschlüsselt.

Beides sind **moderate** Risiken in der Compose-Standardkonfiguration —
das interne Docker-Netz ist isoliert, Postgres hat keinen veröffentlichten
Port. Bei Skalierung (mehrere Backend-Replicas, externe DB, Compliance-
Audits) werden sie aber zu echten Problemen. Diese Anleitung dokumentiert
den Migrationspfad ohne ihn zu erzwingen — die Defaults bleiben so wie
sie für eine Single-VPS-Deployment angemessen sind.

> **Status:** Operator-Aufgabe. Nicht Bestandteil der ausgelieferten
> Container. Wende diese Anleitung an, wenn die Plattform skaliert
> oder eine externe Compliance-Prüfung ansteht.

---

## Stufe 1: Separater Migration-User (privilege-separation)

Heute: App-User hat DDL-Recht und kann jede Tabelle droppen. Bei einem
hypothetischen SQL-Injection-Treffer (alle Queries parametrisiert, daher
extrem unwahrscheinlich) wäre der Schaden maximal.

Ziel: App-User darf nur SELECT/INSERT/UPDATE/DELETE auf den App-Tabellen,
ein separater Migration-User hat das DDL-Recht.

```sql
-- Als Superuser einmalig im laufenden System:
CREATE USER raguser_migrate WITH PASSWORD 'change-me' NOSUPERUSER CREATEDB;
ALTER DATABASE ragdb OWNER TO raguser_migrate;

-- App-User auf minimale Rechte einschränken (alle Tabellen):
REVOKE ALL ON SCHEMA public FROM raguser;
GRANT USAGE ON SCHEMA public TO raguser;
GRANT SELECT, INSERT, UPDATE, DELETE
  ON ALL TABLES IN SCHEMA public TO raguser;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO raguser;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO raguser;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO raguser;

-- Vector-Extension muss vom Migration-User erstellt werden, nicht der App:
SET ROLE raguser_migrate;
CREATE EXTENSION IF NOT EXISTS vector;
```

Backend-Anpassung (`backend/db.py`): aktuell läuft `apply_migrations()`
mit derselben Connection-URL wie der Pool. Für Stufe 1:

1. Neuer Setting-Eintrag in `config.py`: `postgres_migrate_user`,
   `postgres_migrate_password` (optional — wenn leer, läuft alles
   weiter wie bisher).
2. `db.apply_migrations()` öffnet bei gesetztem Migrate-User eine
   separate Verbindung mit dessen Credentials, der Pool nutzt
   weiterhin den App-User.

Migration-Code ist nicht enthalten — bewusst Operator-Entscheidung,
damit ein Update der Plattform nicht silently die DB-Permissions ändert.

## Stufe 2: TLS zwischen Backend und Postgres

Heute: `sslmode` ist nicht gesetzt; libpq-Default `prefer` akzeptiert
plain TCP, wenn der Server keinen Cert anbietet. Innerhalb der Docker-
Bridge praktisch kein Risiko — aber gegen "Container-Nachbar liest
mit" oder spätere externe-DB-Migration unbefriedigend.

### Server-Setup (Postgres-Container)

Ein eigenes Image mit Cert + Key, oder Cert/Key per Volume mounten:

```yaml
# docker-compose.yml (additionssfähig)
postgres:
  image: pgvector/pgvector:pg16
  volumes:
    - ./tls/server.crt:/etc/pg-tls/server.crt:ro
    - ./tls/server.key:/etc/pg-tls/server.key:ro
    - pgdata:/var/lib/postgresql/data
  command:
    - "postgres"
    - "-c"
    - "ssl=on"
    - "-c"
    - "ssl_cert_file=/etc/pg-tls/server.crt"
    - "-c"
    - "ssl_key_file=/etc/pg-tls/server.key"
```

`server.key` muss `0600 postgres:postgres` sein, sonst startet Postgres
nicht. Self-signed Cert reicht innerhalb des Docker-Netzes:

```bash
openssl req -new -x509 -days 3650 -nodes \
  -subj "/CN=postgres" \
  -out tls/server.crt -keyout tls/server.key
chown 999:999 tls/server.key && chmod 600 tls/server.key
```

### Client-Setup (Backend)

`config.py:dsn` erweitern um `sslmode=verify-ca sslrootcert=...` (oder
für Self-Signed innerhalb des Netzes `sslmode=require` — schwächer
gegen MITM, aber innerhalb der Bridge nicht relevant).

```python
return (
    f"host={self.postgres_host} port={self.postgres_port} "
    f"dbname={self.postgres_db} user={self.postgres_user} "
    f"password={self.postgres_password.get_secret_value()} "
    f"sslmode=require"
)
```

## Stufe 3: Externe Managed-DB (z.B. Hetzner Managed-Postgres)

Wenn die Plattform skaliert (mehrere Backends), wird die Single-Compose-
DB irgendwann der Flaschenhals. Eine externe Managed-DB bringt TLS
automatisch mit und ermöglicht Read-Replicas. Siehe
`docs-ops/scaling-500-plus.md` für den größeren Kontext.

In dem Fall werden BEIDE Stufen oben Pflicht — der externe Anbieter
sieht uns sonst nicht als ernstzunehmenden Kunden.
