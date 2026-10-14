/* Generate Entwickler-Dokumentation.docx for the wissenplattform RAG app.
 * Run from the repo root: node build-dev-docs.js
 */
const fs = require("fs");
const path = require("path");
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  Header, Footer, AlignmentType, LevelFormat, HeadingLevel,
  BorderStyle, WidthType, ShadingType, PageNumber, PageBreak,
  TableOfContents, TabStopType, TabStopPosition,
} = require(path.join(process.env.NODE_PATH || "/opt/homebrew/lib/node_modules", "docx"));

const ARIAL = "Arial";
const MONO = "Menlo";
// White-label neutral defaults; override at build time via BRAND_* env.
const APP_NAME = process.env.BRAND_APP_NAME || "Wissensplattform";
const SUB_BRAND = process.env.BRAND_SUB_BRAND || "";
const PRODUCT = SUB_BRAND ? `${APP_NAME} ${SUB_BRAND}` : APP_NAME;
const ACCENT = process.env.BRAND_ACCENT || "2563EB";
const FG = "000000";

const border = { style: BorderStyle.SINGLE, size: 4, color: "CCCCCC" };
const cellBorders = { top: border, bottom: border, left: border, right: border };

const h1 = (t) => new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun(t)] });
const h2 = (t) => new Paragraph({ heading: HeadingLevel.HEADING_2, children: [new TextRun(t)] });
const h3 = (t) => new Paragraph({ heading: HeadingLevel.HEADING_3, children: [new TextRun(t)] });

const p = (text, opts = {}) => new Paragraph({
  children: [new TextRun({ text, ...opts })],
  spacing: { after: 120 },
});

const code = (text) => new Paragraph({
  children: [new TextRun({ text, font: MONO, size: 20 })],
  shading: { fill: "F5F5F5", type: ShadingType.CLEAR },
  spacing: { before: 80, after: 80 },
  border: { left: { style: BorderStyle.SINGLE, size: 12, color: ACCENT, space: 6 } },
});

const codeBlock = (lines) => lines.split("\n").map(l => code(l));

const bullet = (text) => new Paragraph({
  numbering: { reference: "bullets", level: 0 },
  children: [new TextRun(text)],
  spacing: { after: 80 },
});

const numbered = (text) => new Paragraph({
  numbering: { reference: "numbers", level: 0 },
  children: [new TextRun(text)],
  spacing: { after: 80 },
});

function table(headerRow, rows) {
  const colWidths = headerRow.map((_, i) => Math.floor(9000 / headerRow.length));
  // adjust last column to make sum exact
  colWidths[colWidths.length - 1] += 9000 - colWidths.reduce((a,b)=>a+b, 0);

  const mkCell = (text, isHeader, idx) => new TableCell({
    borders: cellBorders,
    width: { size: colWidths[idx], type: WidthType.DXA },
    shading: isHeader ? { fill: "EFEFEF", type: ShadingType.CLEAR } : undefined,
    margins: { top: 80, bottom: 80, left: 120, right: 120 },
    children: [new Paragraph({
      children: [new TextRun({ text: String(text), bold: isHeader, font: isHeader ? ARIAL : undefined })],
    })],
  });

  return new Table({
    width: { size: 9000, type: WidthType.DXA },
    columnWidths: colWidths,
    rows: [
      new TableRow({ children: headerRow.map((c, i) => mkCell(c, true, i)) }),
      ...rows.map(r => new TableRow({ children: r.map((c, i) => mkCell(c, false, i)) })),
    ],
  });
}

const blank = () => new Paragraph({ children: [new TextRun("")] });

// ---- Cover page -----------------------------------------------------
const cover = [
  new Paragraph({
    children: [new TextRun({ text: PRODUCT, size: 56, bold: true })],
    spacing: { before: 2400, after: 240 },
  }),
  new Paragraph({
    children: [new TextRun({ text: "Entwickler-Dokumentation", size: 36, color: "666666" })],
    spacing: { after: 240 },
  }),
  new Paragraph({
    children: [new TextRun({ text: "Internal-Support RAG-Plattform — FastAPI · pgvector · OpenRouter", size: 22, italics: true, color: "666666" })],
    spacing: { after: 1200 },
  }),
  new Paragraph({
    children: [new TextRun({ text: `Stand: ${new Date().toISOString().slice(0,10)}`, size: 20, color: "888888" })],
  }),
  new Paragraph({ children: [new PageBreak()] }),
];

// ---- TOC ------------------------------------------------------------
const toc = [
  h1("Inhaltsverzeichnis"),
  new TableOfContents("Inhalt", { hyperlink: true, headingStyleRange: "1-3" }),
  new Paragraph({ children: [new PageBreak()] }),
];

// ---- Sections -------------------------------------------------------
const content = [];

// 1. Architektur
content.push(h1("1. Architektur-Überblick"));
content.push(p("Die Anwendung ist eine Retrieval-Augmented-Generation-Plattform für interne Unternehmensdokumente. Anwendende stellen Fragen auf Deutsch und erhalten Antworten, die auf hochgeladenen Dokumenten basieren — mit Quellangaben."));
content.push(blank());
content.push(h2("1.1 Drei-Container-Setup"));
content.push(p("Die Plattform besteht aus genau drei Docker-Containern, verbunden über ein privates Docker-Netzwerk. Nur das Frontend veröffentlicht einen Port; Backend und Datenbank sind ausschließlich intern erreichbar."));
content.push(table(
  ["Container", "Image", "Verantwortung", "Exponiert?"],
  [
    ["frontend", "nginx:1.27.3-alpine", "Statische Dateien, Reverse-Proxy auf /api/*", "Ja, Port 8080 → 80"],
    ["backend", "wissenplattform-backend (FastAPI)", "API, Auth, RAG-Pipeline, Admin", "Nein"],
    ["postgres", "pgvector/pgvector:pg16", "Postgres 16 mit pgvector-Extension", "Nein"],
  ]
));
content.push(blank());
content.push(h2("1.2 Stack-Komponenten"));
content.push(bullet("Backend: Python 3.11, FastAPI 0.115, psycopg 3 (async), slowapi (Rate-Limiting)"));
content.push(bullet("Embeddings: intfloat/multilingual-e5-base (768-dim, multilingual, ~280 MB, lokal via sentence-transformers)"));
content.push(bullet("Reranker: BAAI/bge-reranker-v2-m3 (Cross-Encoder, ~570 MB)"));
content.push(bullet("LLM: über OpenRouter (Modell konfigurierbar via OPENROUTER_MODEL)"));
content.push(bullet("Vektor-DB: Postgres 16 + pgvector 0.3.6, HNSW-Index auf chunks.embedding"));
content.push(bullet("Frontend: vanilla JS, kein Build-Step, strikte CSP (kein unsafe-inline, kein eval)"));
content.push(bullet("TLS: Caddy mit Let's-Encrypt (in Produktion vor dem Frontend-Container)"));
content.push(blank());
content.push(h2("1.3 Datenfluss einer Anfrage"));
content.push(numbered("Browser → nginx (TLS via Caddy → 8080) → /api/chat erreicht das Backend."));
content.push(numbered("Backend prüft Session-Cookie und Tageskontingent (chat_usage-Tabelle)."));
content.push(numbered("Bei vorhandenem conversation_id: History aus messages-Tabelle laden. Sonst: neue Conversation anlegen."));
content.push(numbered("Wenn History vorhanden: kleiner LLM-Call rewrites die letzte Frage zu eigenständiger Suchanfrage."));
content.push(numbered("Embedding der (umgeschriebenen) Frage mit „query: \"-Prefix; pgvector retrieve_k=20."));
content.push(numbered("Cross-Encoder-Reranker schneidet auf rerank_top_k=5; sigmoid-Score gegen relevance_threshold=0.3."));
content.push(numbered("OpenRouter-Stream-Completion mit System-Prompt + History + <context>-Block."));
content.push(numbered("SSE-Stream zum Browser; persistiert User- und Assistant-Turn in messages-Tabelle."));

content.push(new Paragraph({ children: [new PageBreak()] }));

// 2. Lokale Entwicklung
content.push(h1("2. Lokale Entwicklung"));
content.push(p("Alles läuft über docker compose. Ein lokaler Python-venv wird nicht unterstützt und nicht benötigt."));
content.push(h2("2.1 Voraussetzungen"));
content.push(bullet("Docker 24+ und Compose v2"));
content.push(bullet("OpenRouter-API-Key (https://openrouter.ai/keys)"));
content.push(bullet(".env aus .env.example kopieren und ausfüllen"));
content.push(blank());
content.push(h2("2.2 Stack starten"));
content.push(...codeBlock("cp .env.example .env\ndocker compose up -d --build"));
content.push(blank());
content.push(h2("2.3 Häufige Befehle"));
content.push(p("Backend nach .env-Änderung neu erstellen (restart reicht NICHT — Compose liest .env nur beim recreate):", { bold: true }));
content.push(code("docker compose up -d --no-deps --force-recreate backend"));
content.push(blank());
content.push(p("Dokumente einlesen (./docs ist als /app/docs gemountet):", { bold: true }));
content.push(code("docker compose exec backend python ingest.py /app/docs"));
content.push(blank());
content.push(p("Admin-User per CLI anlegen:", { bold: true }));
content.push(code("docker compose exec backend python -m auth add-user user@firma.de --admin"));
content.push(blank());
content.push(p("Logs verfolgen:", { bold: true }));
content.push(code("docker compose logs -f backend"));
content.push(blank());
content.push(p("End-to-End-Smoke:", { bold: true }));
content.push(code("./smoke_test.sh"));

content.push(new Paragraph({ children: [new PageBreak()] }));

// 3. Konfiguration
content.push(h1("3. Konfiguration"));
content.push(p("Sämtliche Einstellungen kommen aus Umgebungsvariablen, sind in backend/config.py als pydantic-settings Modell definiert und werden einmalig beim Backend-Start geladen."));
content.push(h2("3.1 Wichtigste .env-Variablen"));
content.push(table(
  ["Variable", "Default", "Bedeutung"],
  [
    ["POSTGRES_DB / USER / PASSWORD", "—", "DB-Credentials (Pflicht)"],
    ["SESSION_SECRET", "—", "HMAC-Secret für Cookies (32+ Hex)"],
    ["OPENROUTER_API_KEY", "—", "Pflicht; Schlüssel von openrouter.ai"],
    ["OPENROUTER_MODEL", "anthropic/claude-3.5-sonnet", "Generationsmodell"],
    ["ALLOWED_ORIGIN", "http://localhost:8080", "CORS-Allowlist (genau 1 Wert, kein *)"],
    ["COOKIE_SECURE", "false", "true setzen sobald hinter HTTPS"],
    ["EMBEDDING_MODEL", "intfloat/multilingual-e5-base", "Wechsel = Migration nötig"],
    ["LOG_LEVEL", "INFO", "DEBUG · INFO · WARNING · ERROR"],
  ]
));
content.push(blank());
content.push(h2("3.2 Hardcoded Settings in config.py"));
content.push(table(
  ["Setting", "Wert", "Zweck"],
  [
    ["embedding_dim", "768", "Muss zu chunks.embedding-Spaltentyp passen"],
    ["retrieve_k", "20", "Wie viele Kandidaten der Bi-Encoder zieht"],
    ["rerank_top_k", "5", "Wie viele am Ende ans LLM gehen"],
    ["relevance_threshold", "0.3", "Sigmoid-Reranker-Score, darunter „kann nicht beantworten\""],
    ["max_history_turns", "10", "Server-Cap auf Konversations-Turns"],
    ["max_history_chars", "8000", "Aggregat-Cap der History-Inhalte"],
    ["chat_daily_limit_per_user", "200", "Tagesbudget pro User"],
    ["bcrypt_cost", "12", "Passwort-Hashing-Aufwand"],
    ["max_failed_logins", "10", "Lockout-Schwelle pro Account"],
    ["lockout_minutes", "15", "Lockout-Dauer"],
  ]
));

content.push(new Paragraph({ children: [new PageBreak()] }));

// 4. Datenbankschema
content.push(h1("4. Datenbankschema"));
content.push(p("Migrationen liegen in backend/migrations/ und werden lexikalisch sortiert beim Backend-Start automatisch angewandt. Tracking erfolgt in schema_migrations."));
content.push(h2("4.1 Tabellen"));
content.push(table(
  ["Tabelle", "Zweck", "Eingeführt in"],
  [
    ["users", "Konten, bcrypt-Hash, Lockout-Counter", "001_init"],
    ["sessions", "Server-side Sessions (sha256-Hash der Cookie-ID)", "001_init"],
    ["login_attempts", "Audit aller Login-Versuche", "001_init"],
    ["documents", "Eingelesene Datei-Metadaten + content-hash", "001_init"],
    ["chunks", "Text-Chunks + 768-dim Embeddings (HNSW-Index)", "001_init / 003"],
    ["schema_migrations", "Welche SQL-Files sind durch", "001_init"],
    ["chat_usage", "Per-User Tageszähler für /api/chat", "004_security_features"],
    ["admin_audit", "Forensik-Log aller Admin-Mutationen", "004_security_features"],
    ["conversations", "Persistente Chats pro User", "005_conversations"],
    ["messages", "User/Assistant-Turns mit Sources (JSONB)", "005_conversations"],
    ["branding", "Singleton-Row mit Markenkonfiguration", "006_branding"],
    ["message_feedback", "👍/👎 + optionaler Kommentar pro Assistant-Nachricht", "007_message_feedback"],
    ["chunks.content_tsv", "Generierte tsvector-Spalte für Hybrid-Search (GIN-Index)", "008_hybrid_search"],
    ["documents.parent_id/version/is_current/superseded_at", "Doc-Versionierung (1 current pro source)", "009_doc_versioning"],
    ["users.totp_secret/totp_enabled · login_challenges", "Optionale 2FA via TOTP + Two-Step-Login", "010_totp"],
  ]
));
content.push(blank());
content.push(h2("4.2 Migration anwenden"));
content.push(p("Migrationen sind self-idempotent (CREATE TABLE IF NOT EXISTS, ALTER … IF NOT EXISTS). Anwenden geschieht beim Backend-Lifespan via apply_migrations() in db.py — vor init_pool(), weil der pgvector-Type-Adapter die vector-Extension benötigt."));

content.push(new Paragraph({ children: [new PageBreak()] }));

// 5. API-Referenz
content.push(h1("5. API-Referenz"));
content.push(h2("5.1 Public"));
content.push(table(
  ["Methode", "Pfad", "Zweck"],
  [
    ["GET", "/health", "Liveness-Probe; { status: ok }"],
    ["GET", "/api/branding", "Markenname/Akzent (für Login-Page)"],
    ["POST", "/api/login", "Email+Passwort → Session-Cookie"],
    ["POST", "/api/logout", "Session invalidieren"],
    ["POST", "/api/login/totp", "Zweiter Login-Schritt: challenge_token + TOTP-Code"],
  ]
));
content.push(blank());
content.push(h2("5.2 Authentifiziert"));
content.push(table(
  ["Methode", "Pfad", "Zweck"],
  [
    ["GET", "/api/me", "Identität + is_admin"],
    ["POST", "/api/chat", "Frage stellen, SSE-Stream zurück"],
    ["GET", "/api/documents", "Liste aktueller Dokumente (für Doc-Viewer)"],
    ["GET", "/api/documents/{id}", "Volltext eines Dokuments inkl. Chunks (auch archivierte)"],
    ["GET", "/api/me/2fa", "Status meines 2FA"],
    ["POST", "/api/me/2fa/setup", "Geheimnis + QR-PNG für Einrichtung"],
    ["POST", "/api/me/2fa/verify", "Erstmals Code prüfen → 2FA aktivieren"],
    ["POST", "/api/me/2fa/disable", "Mit aktuellem Code deaktivieren"],
    ["GET", "/api/conversations", "Eigene Unterhaltungen"],
    ["POST", "/api/conversations", "Neue Unterhaltung erstellen"],
    ["GET", "/api/conversations/{id}", "Volltext-History (inkl. Feedback)"],
    ["PATCH", "/api/conversations/{id}", "Umbenennen"],
    ["DELETE", "/api/conversations/{id}", "Löschen (cascade messages)"],
    ["POST", "/api/messages/{id}/feedback", "👍/👎 + optionaler Kommentar (UPSERT)"],
    ["DELETE", "/api/messages/{id}/feedback", "Bewertung entfernen"],
  ]
));
content.push(blank());
content.push(h2("5.3 Admin (require_admin)"));
content.push(table(
  ["Methode", "Pfad", "Zweck"],
  [
    ["GET", "/api/admin/documents", "Alle Dokumente listen"],
    ["POST", "/api/admin/documents", "Datei-Upload + Inline-Ingest"],
    ["DELETE", "/api/admin/documents/{id}", "Löschen + von Disk entfernen"],
    ["GET", "/api/admin/users", "Alle User listen"],
    ["POST", "/api/admin/users", "User anlegen"],
    ["PATCH", "/api/admin/users/{id}", "is_admin / is_active toggeln"],
    ["POST", "/api/admin/users/{id}/reset-password", "Passwort setzen + Sessions invalidieren"],
    ["DELETE", "/api/admin/users/{id}", "User löschen (cascade)"],
    ["PATCH", "/api/admin/branding", "Markenkonfiguration"],
    ["GET", "/api/admin/analytics", "Aggregierte Metriken fürs Dashboard"],
    ["POST", "/api/admin/eval/retrieve", "Retrieval-Only-Endpoint für Eval-Suite"],
  ]
));
content.push(blank());
content.push(h2("5.4 SSE-Events von /api/chat"));
content.push(table(
  ["Event", "Payload", "Wann"],
  [
    ["meta", "{ conversation_id, is_new }", "Immer als erstes — Client adoptiert die ID"],
    ["sources", "{ sources: [{ source, chunk_index, text }] }", "Vor dem ersten Token"],
    ["token", "{ text }", "Pro Token-Delta vom LLM"],
    ["done", "{}", "Stream regulär beendet"],
    ["error", "{ detail }", "Bei Fehler — generische Meldung"],
  ]
));

content.push(new Paragraph({ children: [new PageBreak()] }));

// 6. RAG-Pipeline
content.push(h1("6. RAG-Pipeline"));
content.push(p("backend/rag.py ist das Herzstück. Vier deutliche Schritte:"));
content.push(blank());
content.push(h2("6.1 Query-Rewrite"));
content.push(p("Wenn History nicht leer: ein kleiner OpenRouter-Call (gleiches Modell wie Generation, max_tokens=80, temperature=0) formuliert die letzte Frage zu einer eigenständigen Suchanfrage um. Beispiel:"));
content.push(...codeBlock('Verlauf: "Was sind die Tier-1 SLA?" → "vier Geschäftsstunden"\nAktuelle Frage: "Und Tier-2?"\n→ Rewrite: "Was sind die Tier-2 SLA?"'));
content.push(blank());
content.push(h2("6.2 Hybrid Embedding + BM25"));
content.push(p("Zwei parallele Pfade, deren Kandidaten anschließend zusammen ans Reranker gehen:"));
content.push(bullet("Dense: multilingual-e5-base, Queries mit „query: \"-Prefix, Passagen mit „passage: \", retrieve_k=20 via pgvector cosine + HNSW."));
content.push(bullet("BM25: chunks.content_tsv (generated tsvector mit 'german'-Wörterbuch), GIN-Index, plainto_tsquery für sichere User-Eingabe, ts_rank_cd-Sortierung, Limit retrieve_k/2=10."));
content.push(blank());
content.push(p("Union by chunk_id (Python dict, dense gewinnt bei Duplikaten weil zuerst eingefügt). Das schließt die typische Schwäche von Dense-Modellen bei Eigennamen, Produktcodes und internen Identifiern, die der Embedder im Training nie gesehen hat."));
content.push(blank());
content.push(h2("6.3 Cross-Encoder-Reranking"));
content.push(p("bge-reranker-v2-m3 erhält (query, chunk)-Paare und gibt Logits zurück. Sigmoid-Normalisierung auf [0,1]. Die top-5 nach Score werden ans LLM weitergereicht. Kosten: ~1-4s auf CPU für 20 Paare."));
content.push(blank());
content.push(h2("6.4 Relevance-Threshold"));
content.push(p("Wenn der höchste Reranker-Score unter relevance_threshold (0.3) liegt, gibt retrieve() eine leere Hit-Liste zurück. Der System-Prompt routet das System dann zur Refusal („Ich kann das anhand der verfügbaren Dokumente nicht beantworten\")."));
content.push(blank());
content.push(h2("6.5 Generation"));
content.push(p("System-Prompt + replayed History (raw, ohne <context>) + finaler User-Turn mit <context>-Block → OpenRouter-Stream. Der System-Prompt instruiert das Modell explizit, <context> als untrusted data zu behandeln (Prompt-Injection-Defense)."));

content.push(new Paragraph({ children: [new PageBreak()] }));

// 6.5 Eval-Suite
content.push(h1("6.5 Eval-Suite"));
content.push(p("Retrieval-Qualität ist eine empirische Frage — „fühlt sich besser an\" reicht nicht. Die Eval-Suite läuft offline gegen einen handkuratierten Gold-Set und liefert nachvollziehbare Metriken."));
content.push(blank());
content.push(h2("Goldset-Format"));
content.push(p("eval/gold_questions.json (von eval/gold_questions.example.json kopieren). Pro Eintrag eine Frage und entweder expected_source (eine Datei) oder expected_sources (Liste; Match wenn IRGENDEINE in den Top-5 auftaucht). Empfohlen: 30-50 Fragen, die das tatsächliche Korpus abdecken."));
content.push(blank());
content.push(h2("Run"));
content.push(...codeBlock('EVAL_BASE=http://localhost:8080 \\\nEVAL_EMAIL=admin@firma.de \\\nEVAL_PASSWORD="..." \\\n  python eval/run_eval.py eval/gold_questions.json'));
content.push(blank());
content.push(h2("Metriken"));
content.push(bullet("Recall@5 — % der Fragen, bei denen die erwartete Quelle in den Top-5 ist"));
content.push(bullet("MRR (Mean Reciprocal Rank) — 1/Position der erwarteten Quelle, gemittelt"));
content.push(bullet("Latenz p50/p95 in ms"));
content.push(blank());
content.push(p("Das Skript ruft /api/admin/eval/retrieve auf — der Endpoint führt nur Retrieval+Reranking aus, KEINE LLM-Generation und KEINE Conversation-Persistenz. Eval-Läufe verbrauchen daher kein OpenRouter-Budget und verfälschen keine User-Daten.", { italics: true }));
content.push(blank());
content.push(p("Exit-Code 0 wenn Recall@5 ≥ 0.7 (heuristische CI-Schwelle), sonst 1. Optional --json-out PATH für strukturierten Report."));

content.push(new Paragraph({ children: [new PageBreak()] }));

// 6.6 Feedback-Schleife
content.push(h1("6.6 Feedback-Schleife"));
content.push(p("Pro Assistant-Nachricht kann der Konversations-Owner ein 👍/👎 vergeben (optional mit Kommentar). Persistiert in message_feedback (PK auf message_id, eine Bewertung pro Nachricht, UPSERT-Semantik). Daten fließen ins Analytics-Dashboard."));
content.push(blank());
content.push(h2("API"));
content.push(bullet("POST /api/messages/{id}/feedback body: { rating: -1|1, comment?: string } — UPSERT"));
content.push(bullet("DELETE /api/messages/{id}/feedback — Bewertung entfernen"));
content.push(blank());
content.push(p("Ownership-Check: nur die User-ID der zugehörigen Conversation darf bewerten. Andere User bekommen 404 (kein info-leak), nicht 403."));

content.push(new Paragraph({ children: [new PageBreak()] }));

// 6.7 Analytics
content.push(h1("6.7 Admin-Analytics"));
content.push(p("GET /api/admin/analytics liefert in einem Call alle Dashboard-Metriken. Die SQL-Queries laufen alle auf einer Connection — Postgres-Side-Aggregation, kein N+1."));
content.push(blank());
content.push(h2("Response-Shape"));
content.push(...codeBlock(`{
  "totals": {users, documents, conversations, messages},
  "recent": {
    conversations_7d, conversations_30d,
    messages_7d, messages_30d,
    active_users_7d, active_users_30d
  },
  "feedback": {thumbs_up, thumbs_down, rated, unrated_assistant_msgs, with_comment},
  "refusal_rate_30d": 0.0-1.0,
  "most_cited_30d": [{source, count}],
  "daily_chats_30d": [{day, count}],
  "login_attempts_7d": {success, failure}
}`));
content.push(blank());
content.push(p("Refusal-Rate: matcht messages.content gegen die kanonische Refusal-Phrase. Wenn dieser Match in der falschen Sprache läuft (z. B. nach System-Prompt-Änderung), zeigt das Dashboard 0 obwohl Refusals stattfinden — Phrase synchron halten mit rag.SYSTEM_PROMPT."));
content.push(blank());
content.push(p("Most-cited: jsonb_array_elements unnest auf messages.sources → group by source. Pro Assistant-Nachricht zählen alle Top-5-Quellen mit, nicht nur die zitierte. Das ist gewollt: ein hoch-relevantes Dokument, das das LLM im Antworttext NICHT zitiert, ist trotzdem im Korpus aktiv und zeigt sich in den Metriken."));

content.push(new Paragraph({ children: [new PageBreak()] }));

// 6.8 Doc-Versioning
content.push(h1("6.8 Doc-Versioning"));
content.push(p("Re-Upload eines Dokuments mit gleichem Dateinamen lehnt nicht mehr mit 409 ab — die alte Version wird automatisch archiviert (is_current=FALSE, superseded_at=now), die neue erhält version=alt+1 und parent_id=alt.id."));
content.push(blank());
content.push(h2("Eindeutigkeit"));
content.push(bullet("Partial Unique-Index documents_one_current_per_source(source) WHERE is_current — verhindert zwei gleichzeitige current rows bei Race-Conditions."));
content.push(bullet("UNIQUE(content_hash) bleibt — Re-Upload identischer Bytes (auch unter neuem Namen) → 409, kein Versionspaar mit gleichem Inhalt."));
content.push(blank());
content.push(h2("Retrieve-Filter"));
content.push(p("Beide SQL-Pfade in rag.retrieve filtern WHERE d.is_current = TRUE. Archivierte Versionen bleiben für Audit/Restore in der DB, sind aber nicht abrufbar."));
content.push(blank());
content.push(h2("Disk-Handling"));
content.push(bullet("Admin-Upload: parse_payload arbeitet im Speicher, DB-Insert läuft zuerst. Erst nach DB-Erfolg wird die Datei geschrieben."));
content.push(bullet("Bei vorhandener current-Datei mit gleichem Namen: rename auf <name>.v{prev}.bak, dann neue Bytes als <name> schreiben. Alte Bytes bleiben für Restore erreichbar."));
content.push(bullet("CLI-ingest archiviert ebenfalls in der DB, schreibt aber nichts auf Disk — der Operator hat die Files eh."));

content.push(new Paragraph({ children: [new PageBreak()] }));

// 6.9 Doc-Viewer
content.push(h1("6.9 Doc-Viewer"));
content.push(p("Klick auf eine Quellenangabe (Pill in der Antwort, oder Snippet-Header im Quelltext-Block) öffnet ein Modal mit dem vollständigen Dokumenttext, der zitierte Abschnitt ist farblich hervorgehoben."));
content.push(blank());
content.push(h2("Wiring"));
content.push(bullet("Backend liefert document_id im SSE sources-Event (additiv zur bestehenden source/chunk_index/text-Struktur)"));
content.push(bullet("Frontend rendert Pill als <button> wenn document_id vorhanden, click → openDocViewer(id, chunk_index, source)"));
content.push(bullet("/api/documents/{id} ist nicht user-scoped — alle authentifizierten User sehen denselben Korpus (per SECURITY.md A01). Archivierte Versionen sind ebenfalls abrufbar, was das Inspizieren historischer Antworten erlaubt."));
content.push(blank());
content.push(h2("Backwards compat"));
content.push(p("Alte Conversations vor diesem Feature haben kein document_id im JSONB sources-Array. Solche Pills bleiben als statisches Span gerendert (nicht klickbar). Neue Conversations sind ab sofort vollständig clickable."));

content.push(new Paragraph({ children: [new PageBreak()] }));

// 7. Sicherheit
content.push(h1("7. Sicherheit"));
content.push(p("SECURITY.md im Repo enthält das vollständige OWASP-Top-10-Self-Review. Hier nur die Eckpunkte für Entwickler:"));
content.push(h2("7.1 Auth & Sessions"));
content.push(bullet("bcrypt cost 12 für Passwort-Hashes"));
content.push(bullet("Session-Cookie: <random_id>.<hmac_sha256(random_id)> — DB speichert nur sha256(random_id)"));
content.push(bullet("Cookie-Flags: HttpOnly, SameSite=Strict, Secure (gated durch COOKIE_SECURE)"));
content.push(bullet("Konstante Login-Zeit auch bei unbekannten Emails (Timing-Attacke verhindert)"));
content.push(bullet("Per-Account-Lockout (10 Versuche / 15 Min) + slowapi Per-IP (5/Min)"));
content.push(bullet("Per-User Tageskontingent für /api/chat (default 200/Tag)"));
content.push(blank());
content.push(h2("7.2 SQL & Prompt-Injection"));
content.push(bullet("Alle SQL-Calls parameterisiert (psycopg %s). Keine f-Strings in SQL."));
content.push(bullet("Pydantic validiert alle Bodies. Chat-Messages: max 2000 Zeichen, Control-Chars gestripped."));
content.push(bullet("Retrieved Context wird in <context>...</context> eingebettet; System-Prompt instruiert das LLM, alles darin als data zu behandeln, nicht als instruction."));
content.push(bullet("User-Frage geht ausschließlich in role:user, NIE im System-Prompt."));
content.push(blank());
content.push(h2("7.3 Headers & CSP"));
content.push(p("SecurityHeadersMiddleware in main.py setzt eine strikte CSP: default-src 'self', kein 'unsafe-inline', kein 'unsafe-eval'. Dieselben Header werden in nginx.conf in jedem location-Block wiederholt — nginx droppt server-level add_header sobald irgendein location-level add_header gesetzt ist (nginx-Footgun)."));
content.push(blank());
content.push(h2("7.4 Audit-Log"));
content.push(p("Jede Admin-Mutation schreibt eine Zeile in admin_audit (actor_id, action, target_type, target_id, details JSONB, ip). actor_id hat ON DELETE RESTRICT — by design: ein Admin, der Aktionen ausgeführt hat, kann nicht einfach gelöscht werden."));
content.push(blank());
content.push(h2("7.5 Zwei-Faktor (TOTP)"));
content.push(p("Optional pro User aktivierbar via /api/me/2fa-Endpoints. Geheimnis ist 32-Char base32 (160 bit per RFC 6238), QR-Code wird serverseitig als PNG-data-URI gerendert (kein externes JS, CSP unverletzt)."));
content.push(blank());
content.push(h2("Login-Flow"));
content.push(bullet("/api/login mit korrektem Passwort: bei totp_enabled=FALSE → 200 + session-Cookie. Bei totp_enabled=TRUE → 202 + challenge_token (kein Cookie)."));
content.push(bullet("Client postet challenge_token + TOTP-Code an /api/login/totp. Bei Match: Challenge gelöscht, Session-Cookie gesetzt."));
content.push(bullet("Challenge-Tabelle login_challenges: gleiche Form wie sessions (id_hash + HMAC-Signatur), TTL 5 Min, single-use. Verlust des zweiten Schritts hinterlässt KEINE half-authed cookies."));
content.push(blank());
content.push(h2("Recovery-Pfad"));
content.push(p("Verlorenes Authenticator-Gerät: Admin setzt Passwort zurück. reset_password (admin.py) löscht totp_secret + setzt totp_enabled=FALSE als Seiteneffekt — User kann mit neuem Passwort allein einloggen, danach 2FA neu aufsetzen. Kein „Backup-Codes\"-Mechanismus zugunsten von Einfachheit."));
content.push(blank());
content.push(h2("Brute-Force-Schutz"));
content.push(p("Falsche TOTP-Codes auf /api/login/totp zählen genauso in users.failed_login_count wie Passwort-Fehlversuche. Lockout greift nach max_failed_logins Versuchen. SlowAPI deckt zusätzlich per-IP-Rate ab (10/min)."));
content.push(blank());
content.push(h2("7.6 Optionaler Malware-Scan (ClamAV)"));
content.push(p("Admin-Upload kann jede Datei vor dem Parsen durch einen ClamAV-Daemon schicken. Aktivierung über Compose-Profile, damit die Plattform auf 4-GB-VPS ohne Scanner ausgeliefert werden kann (~1 GB RAM für Signatur-DB)."));
content.push(...codeBlock("# .env\nCLAMAV_ENABLED=true\n\n# Stack starten\ndocker compose --profile security up -d"));
content.push(blank());
content.push(p("Implementierung: backend/admin.py:_clamd_scan spricht das clamd INSTREAM-Protokoll direkt über asyncio TCP — keine zusätzliche Python-Dep. Bei unerreichbarem clamd wird der Upload abgelehnt (fail-closed mit 503), nicht durchgewinkt."));
content.push(blank());
content.push(p("Footnote: clamav/clamav 1.4.x ist ausschließlich amd64. Lokales Testen auf Apple Silicon braucht platform: linux/amd64 (Rosetta-Emulation) oder den security-Stack auslassen.", { italics: true }));
content.push(blank());

content.push(h2("7.7 Backup & Restore"));
content.push(p("./backup.sh erzeugt pg_dump --format=custom in ./backups/. Optional age-Verschlüsselung via BACKUP_AGE_RECIPIENT. Retention default 14 Tage. Restore:"));
content.push(code("docker compose exec -T postgres pg_restore --clean --if-exists \\"));
content.push(code("  -U \"$POSTGRES_USER\" -d \"$POSTGRES_DB\" < backups/ragdb-...dump"));
content.push(blank());

content.push(h2("7.8 Dependency-Pflege (Renovate)"));
content.push(p(".github/renovate.json fährt PRs für Patch- und Minor-Bumps wöchentlich auf, gruppiert die Python-Deps in eine PR pro Woche und labelt CVE-Fixes mit security. Major-Bumps für FastAPI/Pydantic/sentence-transformers/torch landen mit needs-review-Label und werden NICHT auto-merged. Renovate aktivieren: GitHub-App auf das Repo verbinden, fertig — Config zieht."));
content.push(blank());

content.push(h2("7.9 Log-Shipping"));
content.push(p("docs-ops/log-shipping.md zeigt das minimale Setup für Grafana Cloud (Free-Tier reicht): Loki-Docker-Driver, Credentials in /etc/rag/loki.env, logging-Block pro Service in docker-compose. LogQL-Beispiele für Dashboards und Alerts (Backend-Down, Login-Spike, Quota-Wall, OpenRouter-Errors) sind dort dokumentiert."));

content.push(new Paragraph({ children: [new PageBreak()] }));

// 8. Deployment
content.push(h1("8. Deployment auf Hostinger / VPS"));
content.push(h2("8.1 Empfohlene Plan-Größe"));
content.push(table(
  ["Plan", "RAM", "vCPU", "Eignung"],
  [
    ["KVM 1", "4 GB", "1", "knapp; Reranker ~3-5 s"],
    ["KVM 2", "8 GB", "2", "Sweet Spot für ~20-30 User"],
    ["KVM 4", "16 GB", "4", "komfortabel"],
  ]
));
content.push(blank());
content.push(h2("8.2 Setup-Schritte"));
content.push(numbered("VPS provisionieren (Ubuntu 24.04 LTS empfohlen). Domain-A-Record auf Server-IP setzen."));
content.push(numbered("Docker, Docker Compose, Caddy, ufw installieren."));
content.push(numbered("Repo klonen, .env aus .env.example anlegen mit production-Werten."));
content.push(numbered("POSTGRES_PASSWORD und SESSION_SECRET via openssl rand -hex 32 erzeugen."));
content.push(numbered("OPENROUTER_API_KEY auf openrouter.ai erzeugen und einsetzen."));
content.push(numbered("ALLOWED_ORIGIN=https://deine-domain.de und COOKIE_SECURE=true setzen."));
content.push(numbered("docker compose up -d --build."));
content.push(numbered("Caddy: Caddyfile.example anpassen, an /etc/caddy/ kopieren, systemctl reload caddy."));
content.push(numbered("Erste Dokumente in ./docs/ kopieren, ingest.py laufen lassen."));
content.push(numbered("Erste(n) Admin-User per auth-CLI anlegen."));
content.push(numbered("Impressum + Datenschutz in frontend/impressum.html und frontend/datenschutz.html mit echten Daten ausfüllen, durch Anwalt prüfen lassen."));
content.push(numbered("Backup-Cron einrichten (siehe README §6.7)."));

content.push(new Paragraph({ children: [new PageBreak()] }));

// 9. Bekannte Footguns
content.push(h1("9. Bekannte Footguns"));
content.push(bullet("KEIN from __future__ import annotations in main.py. slowapi's @limiter.limit wraps Routes; FastAPI kann Pydantic-ForwardRefs nicht durch das Wrapper-Globals auflösen → 422 auf jedem Body-Request. Konkrete Annotations + explizites Body(...) verwenden."));
content.push(bullet("docker compose restart liest .env NICHT neu. Nach .env-Änderungen: docker compose up -d --no-deps --force-recreate <service>."));
content.push(bullet("embedding_dim ist hardcoded auf 768. Modellwechsel = neue Migration der chunks.embedding-Spalte UND Re-Ingest."));
content.push(bullet("E5-Prefixes sind verpflichtend: queries → \"query: \", passages → \"passage: \". Ohne Prefix funktioniert es scheinbar, Retrieval-Qualität degradiert still."));
content.push(bullet("CSP-Header in main.py UND nginx.conf in jedem location-Block synchron halten — nginx droppt server-level add_header sobald irgendein location-level add_header existiert."));
content.push(bullet("Reasoning-Modelle (z. B. *-r1, *:reasoning, *thinking*) brauchen viel höheren max_tokens-Cap, sonst leere Antworten. Default 800 reicht für Standard-Modelle."));
content.push(bullet("Die hfcache-Volume muss in backend/Dockerfile mit app:app-ownership pre-created sein, sonst klemmt der erste Modell-Download (Docker-Volume-First-Mount-Verhalten)."));
content.push(bullet("Conversation-Inhalte werden seit Migration 005 persistiert. Datenschutz-Posture entsprechend: SECURITY.md A09 sagt „query content not logged\" — gilt für Server-Logs, NICHT für Conversations."));

const finalContent = [...cover, ...toc, ...content];

const doc = new Document({
  creator: "wissenplattform",
  title: `${PRODUCT} — Entwickler-Dokumentation`,
  styles: {
    default: { document: { run: { font: ARIAL, size: 22 } } },
    paragraphStyles: [
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 36, bold: true, font: ARIAL, color: FG },
        paragraph: { spacing: { before: 360, after: 200 }, outlineLevel: 0 } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 28, bold: true, font: ARIAL, color: FG },
        paragraph: { spacing: { before: 240, after: 140 }, outlineLevel: 1 } },
      { id: "Heading3", name: "Heading 3", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 24, bold: true, font: ARIAL, color: FG },
        paragraph: { spacing: { before: 180, after: 100 }, outlineLevel: 2 } },
    ],
  },
  numbering: {
    config: [
      { reference: "bullets", levels: [{ level: 0, format: LevelFormat.BULLET, text: "•",
        alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 720, hanging: 360 } } } }] },
      { reference: "numbers", levels: [{ level: 0, format: LevelFormat.DECIMAL, text: "%1.",
        alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 720, hanging: 360 } } } }] },
    ]
  },
  sections: [{
    properties: {
      page: {
        size: { width: 11906, height: 16838 }, // A4
        margin: { top: 1440, right: 1440, bottom: 1440, left: 1440 },
      },
    },
    headers: {
      default: new Header({ children: [new Paragraph({
        alignment: AlignmentType.RIGHT,
        children: [new TextRun({ text: `${PRODUCT} — Entwickler-Dokumentation`, color: "888888", size: 18 })],
      })] }),
    },
    footers: {
      default: new Footer({ children: [new Paragraph({
        alignment: AlignmentType.CENTER,
        children: [
          new TextRun({ text: "Seite ", color: "888888", size: 18 }),
          new TextRun({ children: [PageNumber.CURRENT], color: "888888", size: 18 }),
        ],
      })] }),
    },
    children: finalContent,
  }],
});

Packer.toBuffer(doc).then((buf) => {
  fs.writeFileSync("Entwickler-Dokumentation.docx", buf);
  console.log("ok: Entwickler-Dokumentation.docx (" + buf.length + " bytes)");
});
