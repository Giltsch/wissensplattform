# Verzeichnis der Verarbeitungstätigkeiten (VVT)

Vorlage nach **DSGVO Art. 30 Abs. 1** für den Betreiber dieser Plattform.
Eckige Klammern sind durch echte Daten zu ersetzen; das Verzeichnis wird
vom Verantwortlichen geführt und auf Anfrage der Aufsichtsbehörde
vorgelegt (Art. 30 Abs. 4 DSGVO). Keine Veröffentlichungspflicht — diese
Datei ist nicht für die Auslieferung ans Frontend gedacht.

> **Status:** Template. Vor Go-Live durch den Datenschutzbeauftragten /
> die Datenschutzbeauftragte freigeben lassen.

---

## 1. Verantwortlicher

| Feld | Inhalt |
|---|---|
| Name | [Firma] |
| Anschrift | [Straße, PLZ, Ort] |
| Telefon | [Tel] |
| E-Mail | [datenschutz@example.de] |
| Vertretungsberechtigte:r | [Person / Rolle] |

## 2. Datenschutzbeauftragte:r

| Feld | Inhalt |
|---|---|
| Name | [DSB] |
| Kontakt | [E-Mail / Telefon] |
| Bestellt am | [Datum] |

## 3. Bezeichnung der Verarbeitungstätigkeit

> Betrieb der internen Wissensplattform „Wissensplattform" — RAG-basierter
> Chatbot zur Beantwortung von Mitarbeiterfragen anhand interner Dokumente.

## 4. Zwecke der Verarbeitung

- Bereitstellung eines internen Such- und Frage-Antwort-Werkzeugs
- Authentifizierung und Zugriffssteuerung (rollen- und tag-basiert)
- IT-Sicherheit (Erkennung von Brute-Force, Quota-Überschreitungen)
- Qualitätssicherung (anonymisiertes Aggregat-Feedback der Antworten)

## 5. Rechtsgrundlage

| Verarbeitung | Rechtsgrundlage |
|---|---|
| Kontoverwaltung, Konversationsspeicherung | Art. 6 Abs. 1 lit. b DSGVO (Vertrag/Arbeitsverhältnis) |
| Login-Audit, Quota, Admin-Audit | Art. 6 Abs. 1 lit. f DSGVO (berechtigtes Interesse: IT-Sicherheit) |
| Übermittlung an Sprachmodell-Anbieter | Art. 6 Abs. 1 lit. f + Art. 28 + Art. 46 Abs. 2 lit. c DSGVO (SCC 2021/914) |
| CRM-Import (optional, anonymisiert) | [Betreiber: Art. 6 Abs. 1 lit. b/f DSGVO — je nach Zweck; Anonymisierung/Pseudonymisierung als TOM] |

## 6. Kategorien betroffener Personen

- Beschäftigte des Verantwortlichen
- ggf. Externe mit explizit eingerichtetem Konto

## 7. Kategorien personenbezogener Daten

| Kategorie | Beispiele |
|---|---|
| Konto-Stammdaten | E-Mail-Adresse, Rolle, Aktivstatus, Erstellungsdatum |
| Authentifizierungsdaten | bcrypt-Passworthash (Cost 12), TOTP-Secret, Recovery-Codes (gehasht), Session-Hashes |
| Aktivitätsdaten | Konversations-Inhalte (Frage + Antwort + Quellen), Tageskontingent, Feedback (👍 / 👎) |
| Sicherheitsdaten | Login-Versuche (Zeitpunkt, IP, Erfolg), Admin-Audit (Akteur, Aktion, Ziel, IP, Zeitpunkt) |

## 8. Kategorien von Empfängern

| Empfänger | Rolle | Sitz | Rechtsgrundlage Drittland |
|---|---|---|---|
| OpenRouter Inc. | Auftragsverarbeiter (Routing) | USA | SCC 2021/914 + ggf. DPF |
| [Sprachmodell-Anbieter — z.B. Anthropic PBC] | Sub-Auftragsverarbeiter | [USA / EU] | [DPF / EU — entfällt] |
| Hosting-Anbieter VPS | Auftragsverarbeiter (Infrastruktur) | [DE / EU] | n/a |
| Backup-Speicher | Auftragsverarbeiter | [Standort] | [SCC falls Drittland] |

## 9. Übermittlung in Drittländer

| Empfänger | Land | Garantie |
|---|---|---|
| OpenRouter Inc. | USA | EU-Standardvertragsklauseln 2021/914, Modul 2; verschlüsselte Übertragung TLS 1.2+ |
| [Sprachmodell-Anbieter] | [USA / EU] | [DPF-Zertifizierung / SCC / entfällt bei EU-Anbieter] |

## 10. Löschfristen

| Datenkategorie | Frist | Mechanismus |
|---|---|---|
| Sessions | 7 Tage nach Ablauf | `cleanup.py` |
| Login-Challenges | 1 Tag nach Ablauf | `cleanup.py` |
| Login-Versuche | 90 Tage | [TODO: cleanup-Erweiterung] |
| Tageskontingent | 30 Tage rollierend | [TODO: cleanup-Erweiterung] |
| Konversationen / Nachrichten | Kontolaufzeit; Self-Delete via `DELETE /api/me` oder pro Unterhaltung | Datenbank + Self-Service |
| Admin-Audit | [1 Jahr; GoBD-Pflicht prüfen] | [TODO: cleanup-Erweiterung] |
| Backups | 14 Tage | `backup.sh` Rotation |

## 11. Technische und organisatorische Maßnahmen (TOM)

Detail: siehe `SECURITY.md` (OWASP-Top-10 Selbst-Review).
Kurzfassung:

- Verschlüsselung: TLS 1.2+ in Transit (Caddy); bcrypt für Passwörter; AEAD-Verschlüsselung für TOTP-Secrets [TODO laut Audit C6-H3]; verschlüsselte Backups (`age`, [TODO als Default erzwingen])
- Authentifizierung: Pflicht-Login, optional TOTP-2FA, Account-Lockout, per-IP-Rate-Limiter
- Autorisierung: tag-basierte ACL (Migration 011/012), kwarg-only-Parameter in `retrieve()`
- Pseudonymisierung: Session-IDs gehasht, kein Plaintext-Log von Anfragen/Inhalten
- Integrität: Admin-Audit-Log, parametrisierte SQL, CSP-Reports
- Verfügbarkeit: tägliche DB-Backups, Health-Check, Auto-Restart
- Belastbarkeit: Retry/Fallback bei LLM-Ausfall, Rate-Limits
- Wiederherstellbarkeit: dokumentierter Restore-Prozess [TODO Restore-Drill]
- Verfahren zur regelmäßigen Überprüfung: CI mit pip-audit, jährliches Security-Review

## 12. Datenschutz-Folgenabschätzung (Art. 35)

Eine DSFA wurde geprüft am [Datum]. Ergebnis: [erforderlich / nicht
erforderlich]. Begründung: [knapp].
Bei Bejahung: Verweis auf das DSFA-Dokument.

## 12a. Teil-Verarbeitungstätigkeit: CRM-/Fremdsystem-Import (optional)

> Nur relevant, wenn die CRM-Anbindung aktiviert ist (`CRM_ENABLED=true`).
> Standard: deaktiviert. Eigenständige Verarbeitungstätigkeit nach Art. 30
> DSGVO, weil sie eine andere Datenquelle, andere Betroffenenkategorien und
> eine eigene Rechtsgrundlage hat.

| Feld | Inhalt |
|---|---|
| Bezeichnung | Stapel-Import anonymisierter CRM-Datensätze in den Wissensindex |
| Zweck | Beantwortung von Mitarbeiterfragen zu Kunden-/Vorgangsdaten auf Basis anonymisierter Auszüge |
| Quelle | Externes CRM-/Fremdsystem über **nur-lesenden** (SELECT-only) Datenbankzugang; keine Live-Abfrage, kein Rückschreiben |
| Betroffenenkategorien | [Betreiber eintragen — z. B. Kunden, Interessenten, Ansprechpartner] |
| Datenkategorien (nach Anonymisierung) | Pseudonyme (HMAC, nicht umkehrbar), als nicht-personenbezogen eingestufte Strukturfelder, NER-geschwärzte Freitexte |
| Rechtsgrundlage | [Betreiber: Art. 6 Abs. 1 lit. b/f DSGVO] |
| Empfänger / Drittland | Wie Haupttätigkeit: anonymisierter Text geht ggf. an den Sprachmodell-Anbieter (Drittlandtransfer nach SCC), **entfällt** bei lokal gehostetem Modell (vLLM/Ollama) |
| TOM (spezifisch) | Anonymisierung/Pseudonymisierung vor Speicherung (`backend/pii.py`), fail-closed-Tagging (`crm_unmapped`, niemandem zugeordnet), Audit nur mit Anzahlen, getrennter Sync-Container ohne offenen Port |
| Löschfristen | Versionierung im Dokumentindex; **offen (Fast-Follow):** Tombstone-Lauf für Recht-auf-Löschung (Art. 17), wenn ein Datensatz upstream verschwindet — siehe SECURITY.md „NOT done" |

## 13. Stand / Versionierung

| Version | Datum | Bearbeiter:in | Änderung |
|---|---|---|---|
| 1.0 | [YYYY-MM-DD] | [Name] | Erstanlage |
