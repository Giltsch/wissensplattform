# Skalierungs-Strategie: 500+ aktive Nutzer

Dieses Dokument ist die Roadmap, um die Wissensplattform von ihrem
heutigen Zustand (10–20 gleichzeitig aktive Nutzer auf einem 4-vCPU-VPS)
auf 500+ zu heben. Es ist explizit kein Implementierungs-Plan, sondern
ein Entscheidungs-Dokument: *welche* Engpässe in welcher Reihenfolge
fallen müssen, *was wir nicht tun sollten*, und *wann* ein Schritt sich
lohnt.

Quelle für die heutigen Messwerte: `CLAUDE.md` (Latency-Breakdown
Mai 2026), `backend/db.py`, `backend/rag.py`, `backend/main.py`.

---

## 0. Was 500+ Nutzer eigentlich bedeutet

Bevor wir skalieren, müssen wir die Frage präzisieren. „500 Nutzer" ist
nicht eine Zahl, sondern drei verschiedene:

| Begriff | Bedeutung | Aktuelle Kapazität |
|---|---|---|
| **Registriert** | Accounts in `users`-Tabelle | praktisch unbegrenzt (DB-Limit) |
| **Eingeloggt parallel** | Aktive Sessions, „auf der Seite" | ~500 schon heute kein Problem (async I/O) |
| **Gleichzeitig aktiv chattend** | Innerhalb desselben ~5-Sekunden-Fensters auf „Senden" | **heute ~4–8, Ziel: ~50** |

Die ehrliche Zielgröße für „die Plattform trägt 500 Nutzer" ist:

- **500 registrierte Accounts**
- **~50 gleichzeitig *aktiv chattende* Nutzer** im Burst
- **~200 gleichzeitig eingeloggte** Nutzer mit Browse-Aktivität
- 95-Perzentil-TTFT < 8 s, Fehlerrate < 1 %

Mit diesen Zahlen rechnen wir im Folgenden.

---

## 1. Wo die Wände stehen

Bevor wir Stellschrauben drehen, müssen wir die Engpässe namentlich
kennen. In der Reihenfolge, in der sie greifen:

### 1.1 Cross-Encoder-Reranker (heute ~3 s pro Anfrage, CPU-gebunden)

Der `bge-reranker-v2-m3` läuft in einem Worker-Thread (`anyio.to_thread`)
und ist seit dem letzten Tuning auf `torch.set_num_threads(1)` gesetzt.
Damit skaliert er **linear mit verfügbaren CPU-Cores**: 4 Cores = 4
parallele Reranks. Auf 50 gleichzeitige Burst-Anfragen brauchen wir
~12 Cores im Backend-Prozess — das ist die erste harte Wand.

### 1.2 OpenRouter-Rate-Limit (alle teilen einen API-Key)

OpenRouter limitiert pro Account. Bei 50 parallelen LLM-Calls reicht die
Standard-Quote nicht mehr; 429-Antworten häufen sich. Mit dem heute
eingebauten Retry + Fallback-Modell überleben wir 10–20 — aber kein 50,
weil dann auch der Fallback-Provider sein Limit erreicht.

### 1.3 LLM-Kosten

Bei 500 aktiven Nutzern × 5 Fragen/Tag × ~2 000 Tokens/Antwort
≈ **5 Mio. Tokens/Tag**. Bei Haiku-Preisen ($1 in / $5 out) sind das
etwa **15–25 €/Tag**, also 450–750 €/Monat — kein Drama, aber
Buchungsthema. Bei Sonnet-Preisen das Fünffache.

### 1.4 Postgres-Connection-Pool

Heute 20 Connections. Bei 200 gleichzeitig eingeloggten Nutzern, die
History/Conversations browsen, queuet das. Lösbar.

### 1.5 Embedder + Reranker in jeder Backend-Replica

~850 MB RAM pro Replica. Wenn wir auf 4 Backend-Replicas hochgehen, sind
das 3,4 GB *nur* für Modelle — Verschwendung, weil dieselben Gewichte
viermal im Speicher liegen. Das ist die zweite Wand, die uns zwingt,
Inference und API zu trennen.

### 1.6 SlowAPI-Rate-Limiter (in-memory pro Prozess)

Sobald wir > 1 Worker oder > 1 Replica fahren, zählt jeder Prozess seine
eigenen Buckets. Die im Code definierten Limits (`30/minute` für Chat,
`5/minute` für Login) werden de facto pro Replica gemultipliziert. Bei
4 Replicas wird aus „30 Chats/Min pro IP" → „120 Chats/Min pro IP".
Brauchbar bis ~2 Replicas, danach Redis-backend nötig.

### 1.7 Login-Storm (Bcrypt cost=12)

Bcrypt cost=12 ist ~250 ms pro Login. Bei 100 Nutzern, die um 09:00
gleichzeitig den Tab öffnen, blockiert das den einzigen Worker für
~25 s. Lösbar durch Worker-Anzahl oder SSO mit Token-Reuse.

### 1.8 ACL- und Audit-Tabellen

Funktional skaliert beides — aber `admin_audit` wächst monoton.
Bei 500 Nutzern und aktivem Admin-Betrieb sind das in einem Jahr ein paar
Millionen Zeilen. Archivierungs-Strategie wird nötig.

### 1.9 Kein HA / kein Failover

Heute single VPS, single Postgres, single Backend. Bei 500 Nutzern, die
auf die Plattform für Wissensarbeit zugreifen, ist ein vollständiger
Ausfall kein „kurzer Restart", sondern ein Produktivitäts-Event mit
Reporting-Pflicht. HA wird Pflicht, nicht Kür.

---

## 2. Phasen-Plan

Drei Phasen, jeweils mit klarem „Wann gehen wir die nächste an?"-
Kriterium. Wir gehen nur weiter, wenn die heutige Phase *gemessen* an
ihr Limit kommt — nicht „weil 500 ja kommen könnten".

### Phase 1 — Vertikal härten (Ziel: ~50 gleichzeitig aktiv)

**Auslöser zum Eintritt:** mehr als 10 gleichzeitig aktive Chats im
P95-Tagesschnitt, oder Workshop-Termin mit >20 Personen.

**Was wir tun:**

1. **VPS auf 8 vCPU / 16 GB hochziehen** (z. B. Hetzner CCX23 mit
   dedizierten vCPU für ~30 €/Monat — geteilte vCPUs auf CX-Linie
   reichen für CPU-gebundene Inference *nicht* aus, weil Noisy
   Neighbours den Rerank in die Länge ziehen).
2. **Postgres-Tuning:** `shared_buffers=2GB`, `work_mem=32MB`,
   `effective_cache_size=10GB`. Heute laufen wir auf Defaults
   (`shared_buffers=128MB`) — das ist für die Größe ein Witz.
3. **DB-Pool auf 50** in `backend/db.py`.
4. **Redis für SlowAPI** als Storage-Backend einbauen. Dann zählen
   alle Buckets global, und wir können später Replicas hinzufügen,
   ohne dass Rate-Limits per Replica vervielfacht werden.
5. **OpenRouter auf bezahlten Tier** mit höherem RPM (oder zwei
   API-Keys gegen verschiedene Provider, vom Backend rotiert).
6. **OTel-Exporter scharfschalten** — `OTEL_EXPORTER_OTLP_ENDPOINT`
   auf Grafana Cloud Tempo o.ä. setzen. Ohne Traces fahren wir ab
   ~50 Usern blind.

**Nicht-Aktionen:** Backend-Replicas, separate Services, K8s.

**Erwartete Kapazität nach Phase 1:** 50 gleichzeitig aktive Chats
bei ~6 s P95-TTFT. Kosten: ~50 €/Monat Infrastruktur + LLM-Kosten
nach Nutzung.

---

### Phase 2 — Inference vom API-Backend trennen (Ziel: ~200 gleichzeitig aktiv)

**Auslöser zum Eintritt:** Phase-1-Setup zeigt im Monitoring durchgehend
> 70 % CPU im Backend, oder geplante Nutzerbasis überschreitet 300
registrierte Accounts.

Der zentrale Schnitt ist hier: **Embedder und Reranker aus dem
API-Backend rausziehen.** Sie werden ein eigener Service, den mehrere
Backend-Replicas gemeinsam nutzen.

**Was wir tun:**

1. **`inference`-Service als eigener Container:** FastAPI/HTTP, zwei
   Endpoints `/embed` und `/rerank`. Lädt Modelle einmal, hält sie
   im RAM, beantwortet Anfragen aus mehreren Backend-Replicas.
   `rag.py` ruft via httpx-Pool auf, statt in-process zu rechnen.
2. **2–4 Backend-Replicas** hinter einem Reverse-Proxy (nginx ist
   schon da, einfach `upstream` mit mehreren Targets). Pro Replica
   nur noch ~150 MB RSS, weil keine Modelle drin.
3. **PgBouncer in Transaction-Mode** zwischen Backends und Postgres.
   Jedes Backend „sieht" 50 Connections, real teilen sie sich 50
   Server-Connections. Wichtig: Transaction-Mode bricht
   prepared statements und session-level features — `psycopg` benutzt
   prepared statements per Default; wir müssen `prepare_threshold=0`
   in der Pool-Config setzen oder PgBouncer auf Session-Mode lassen
   und stattdessen den Pool pro Backend kleiner halten.
4. **Reranker-Service horizontal skalieren:** 2–4 Replicas vom
   Inference-Service, eigenes Load-Balancing. Das ist der eigentliche
   Hebel — der Reranker bleibt der Engpass.
5. **Multi-Provider-Router für LLM-Calls:** Anthropic, Mistral,
   OpenAI über die OpenRouter-API parallel; Load-Balancing nach
   verbleibendem Budget pro Key + nach 429-Historie.
6. **Sessions / Login-Lockout-Zähler nach Redis** verlagern. Heute
   leben sie in Postgres (`login_failures` etc.); bei Login-Storms
   wird das ein Hotspot. Redis ist ohnehin schon im Stack durch
   SlowAPI.
7. **Read-Replica für Postgres** aufsetzen. Retrieval-Queries gehen
   gegen die Replica (eventually consistent reicht für Suche),
   Writes (audit, conversations, messages) gegen die Primary.

**Architektur nach Phase 2:**

```
                       ┌────────────┐
   Browser → Caddy → nginx → backend × 2-4 ─┬─→ inference × 2-4 (CPU)
                                            ├─→ Postgres-Primary ←─ PgBouncer
                                            ├─→ Postgres-Replica (read)
                                            └─→ Redis (sessions, rate-limits)
                                            └─→ OpenRouter (multi-key)
```

**Erwartete Kapazität:** 200 gleichzeitig aktive Chats, P95-TTFT
~5 s. Kosten: 2–3 VMs (Backend + Inference + DB) à 30–50 €/Monat
= ~120 €/Monat Infrastruktur.

**Nicht-Aktionen:** Kubernetes. Wir haben jetzt 3 Services und ~5
Container — das fährt Docker Compose mit `--scale` immer noch
problemlos auf zwei VMs.

---

### Phase 3 — Produktions-Reife (Ziel: 500+ aktiv, HA, SSO)

**Auslöser zum Eintritt:** Phase 2 läuft stabil, und wir bekommen
SLA-relevante Anforderungen (z. B. „Wissensplattform darf nicht
ausfallen, wenn die Schichtübergabe um 14:00 stattfindet").

**Was wir tun:**

1. **Reranker auf GPU.** Eine einzelne GPU (z. B. Hetzner GEX44 mit
   RTX-4000 oder Cloud-GPU) ersetzt 8–12 CPU-Cores für Rerank, bei
   30–50 €/Monat. Latenz pro Rerank fällt von 3 s auf 50–100 ms.
   *Das ist der größte Einzel-Hebel auf der ganzen Roadmap* — der
   gesamte Rerank-Engpass verschwindet.
2. **Postgres als managed Service** (Hetzner Managed PostgreSQL,
   Aiven, oder Self-hosted mit `patroni` + `etcd` für Failover).
   Streaming-Replication, automatische Backups, WAL-Archivierung.
3. **Object-Storage für Dokumente** (S3-kompatibel, z. B. Hetzner
   Object Storage). Heute liegen Original-PDFs im Container; bei
   100+ Dokumenten und Versionierung wird das unhandlich. DB hält
   nur Metadaten + Chunks.
4. **OIDC-SSO** statt lokaler Accounts. Bei 500 Nutzern ist das
   `auth add-user`-CLI-Modell nicht mehr tragbar. Keycloak oder
   die Auth-Komponente des Kunden (Microsoft Entra, Google
   Workspace). Lokale Accounts bleiben für Admin-Notfälle.
5. **Pro-Tenant-Budgets und Quoten** (statt nur pro-User-Tageslimit).
   Wenn die Plattform für mehrere Abteilungen genutzt wird, will
   jede ihre eigenen Kostenrahmen sehen und durchsetzen können.
6. **Vollständige Observability:** Tempo (Traces) + Prometheus
   (Metrics) + Loki (Logs) + Alerts (CPU, P95-TTFT, 5xx-Rate, LLM-
   Kosten, Quote pro Tenant).
7. **Eval-Suite nightly.** Bei wachsendem Korpus driftet die
   Retrieval-Qualität. `eval/`-Ordner gibt es schon — den auf eine
   nächtliche CI-Pipeline mit Tracking heben.

**Erwartete Kapazität:** 500+ aktiv, P95-TTFT < 4 s (dank GPU),
Verfügbarkeit > 99,5 %. Kosten: 300–500 €/Monat Infrastruktur,
LLM-Kosten je nach Nutzung 500–2 000 €/Monat.

---

## 3. Wann (und ob) Kubernetes?

Antwort vorweg: **frühestens in Phase 3, und auch dann nur, wenn ein
zweiter Grund dazukommt.**

K8s lohnt sich, wenn *mindestens zwei* der folgenden Bedingungen wahr
sind:

- ≥ 5 unabhängige Services, die unabhängig deployed werden
- ≥ 2 Umgebungen (Stage + Prod), die strukturgleich sein sollen
- Plattform-Team (≥ 1 Person), die K8s pflegt
- Bedarf an Self-Healing über Region-Grenzen hinweg
- Compliance-Anforderung an deklarative Infrastruktur

Diese App erfüllt selbst in Phase 3 nur 2–3 dieser Kriterien. Die
realistischen Alternativen:

| Option | Eignung |
|---|---|
| **Docker Compose auf 2–3 VMs** + Caddy + managed Postgres | Bis ~300 Nutzer optimal — niedrigste Komplexität |
| **HashiCorp Nomad** | Ähnliche Funktionalität wie K8s, ⅓ der Komplexität |
| **Fly.io / Render / Hetzner Cloud Apps** | Managed Container-Hosting, billig, ausreichend bis ~500 |
| **Kubernetes (Hetzner Cloud Control / k3s)** | Erst wenn Plattform-Team da ist |

Die Hardware-Effizienz von K8s ist *schlechter* als Compose auf großen
VMs (Overhead durch kube-Komponenten, Pod-Network), nicht besser. Der
Vorteil liegt rein in Deployment-Workflows und Self-Healing — Dinge,
die wir uns mit gut betreuten Systemd-Units + Healthchecks auch holen.

---

## 4. Querschnitts-Themen, die mit jeder Phase mitwachsen müssen

### 4.1 Sicherheit

`SECURITY.md` ist heute auf Single-Tenant + interne Nutzer ausgelegt.
Ab Phase 2:

- Penetrationstest vor Phase-3-Go-Live
- DPA und ROPA aktualisieren (Auftragsverarbeitung mit OpenRouter,
  ggf. mit GPU-Provider)
- CSP-Report-Endpoint scharfschalten (heute Header gesetzt, aber kein
  Empfänger)
- Audit-Log-Archivierung nach 90 Tagen ins Cold-Storage
- Backup-Restore-Drill quartalsweise

### 4.2 Kosten-Kontrolle

Ab Phase 2 wird LLM der dominierende Kostenfaktor. Maßnahmen:

- Pro-User-Tageslimit (`chat_daily_limit_per_user`) bleibt; aber wir
  brauchen pro-Tenant-Limits.
- Caching der LLM-Antworten für identische Frage + identische
  Retrieval-Treffer (Hash über Question + Hit-IDs). Spart bei FAQ-
  artiger Nutzung erfahrungsgemäß 20–40 %.
- Routing-Strategie: einfache Fragen an Haiku, komplexe an Sonnet.
  Klassifizierung über Token-Anzahl der Frage + Anzahl Retrieval-
  Treffer ist eine ausreichende Heuristik für den Start.

### 4.3 Observability

Pflicht-Dashboards:

1. **TTFT-Histogram** (P50/P95/P99), nach Modell aufgeschlüsselt
2. **Rerank-Latenz**, mit Queue-Tiefe
3. **OpenRouter-Status-Codes** (Verteilung nach Modell und Key)
4. **DB-Pool-Auslastung** (heute hardcoded, will man pro Replica
   sehen)
5. **Tokens/Tag und Kosten/Tag** pro Tenant
6. **Login-Fehlerrate** (Sicherheits-Indikator)

### 4.4 Datenhaltung

`conversations` und `messages` wachsen monoton. Bei 500 Nutzern × 30
Fragen/Monat × 12 Monate = 180 000 Nachrichten/Jahr. Postgres trägt
das problemlos, aber:

- TTL-Policy festlegen (z. B. „Konversationen älter als 1 Jahr werden
  archiviert"). Datenschutz-Argument *und* DB-Größen-Argument.
- Korpus selbst (`documents`, `chunks`) muss eine Versions- und
  Lösch-Policy haben. Heute gibt es `is_current = TRUE` — Archiv-
  Versionen bleiben für immer, wachsen unbegrenzt.

---

## 5. Entscheidungs-Punkte (Reihenfolge der nächsten 6 Monate)

Dies ist kein Pflicht-Plan, sondern eine Checkliste der Punkte, an
denen wir uns bewusst entscheiden müssen:

1. **Sofort:** Phase 1 umsetzen (VPS hochziehen + Postgres-Tuning +
   Redis für SlowAPI + OTel scharfschalten). Aufwand: 1–2 Tage.
2. **Bei 30+ registrierten Nutzern:** Eval-Suite formal aufsetzen,
   damit wir Retrieval-Drift erkennen.
3. **Bei 50+ registrierten oder einem geplanten Workshop > 30 Pers.:**
   Phase 2 starten. Inference-Service als erste Aufgabe, danach
   Backend-Replicas.
4. **Bei vertraglicher SLA-Anforderung oder ≥ 200 registrierten:**
   Phase 3 starten. GPU für Reranker zuerst (größter Hebel),
   managed Postgres zweitens, SSO drittens.
5. **Niemals:** K8s nur, weil 500 Nutzer „groß" klingt. Erst, wenn
   einer der harten K8s-Trigger aus §3 erfüllt ist.

---

## 6. Zusammenfassung in einem Satz

Der einzige Architektur-Schnitt, den 500 Nutzer wirklich erzwingen, ist
**Inference vom API-Backend trennen**; alles davor ist Vertikal-
Skalierung, alles danach ist Produktions-Härtung. Kubernetes ist auf
der ganzen Strecke nicht der Engpass und nicht die Lösung.
