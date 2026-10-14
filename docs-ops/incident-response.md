# Incident-Response-Plan

Audit-Findings C4-LOW-14 + C6-LOW-13 (Art. 33/34 DSGVO Meldepflicht):
Im Ernstfall ist 72-Stunden-Frist nicht ad-hoc einhaltbar — Eskalations-
baum, Vorlagen und Beweissicherung müssen vorbereitet sein.

> **Status:** Template. Vor Go-Live durch DSB/Justiziariat freigeben.
> Jährlicher Tabletop-Test dokumentieren.

---

## 0. Rollen

| Rolle | Verantwortung | Person |
|---|---|---|
| Incident-Lead | Koordiniert IR-Schritte, kommuniziert intern | [Operator] |
| Datenschutzbeauftragte:r | Entscheidet über Meldung an Aufsicht | [DSB] |
| Rechtsberatung | TMG/UWG/Vertragsrechtsfragen | [Justiziariat] |
| Tech-Lead | Technische Eindämmung, Forensik | [Operator] |
| Kommunikation | Externe Stellungnahme | [Geschäftsführung] |

24/7-Erreichbarkeit: [Telefon-Liste pflegen, off-band].

## 1. Klassifikation (innerhalb 1h ab Erkennung)

| Klasse | Beispiel | Auswirkung | Eskalation |
|---|---|---|---|
| C1 — kritisch | DB-Leak, Admin-Account-Übernahme, RCE | Personenbezug + viele Betroffene | DSB + Aufsicht + GF |
| C2 — hoch | Einzel-Account-Übernahme, Auth-Bypass-Bug | Wenige Betroffene | DSB-Konsultation |
| C3 — mittel | DoS, falsch konfigurierte ACL | Verfügbarkeit / Vertraulichkeit eingeschränkt | Tech-Lead |
| C4 — niedrig | Bekannter CVE, kleines Misconfig | Theoretisch ausnutzbar | nächste Routine |

## 2. Erstreaktion (innerhalb 4h)

**C1/C2 sofortige Schritte:**

1. **Beweise sichern** *bevor* Container/VM neugestartet werden:
   - `docker compose logs --no-color --tail=10000 > /tmp/ir-$(date +%FT%T).log`
   - DB-Snapshot: `./backup.sh` außer der Reihe
   - Disk-Snapshot via Cloud-Provider falls möglich
   - `iptables -L`, `netstat -tunap`, `ss -s`, `last`, `who` — Hostzustand
2. **Eindämmen:**
   - `OPENROUTER_API_KEY` rotieren (kostet sonst Geld bei aktivem Exploit)
   - `SESSION_SECRET` rotieren → alle Cookies invalidiert (alle User
     loggen sich neu ein)
   - Bei Admin-Compromise: betroffenen User direkt in DB deaktivieren:
     `UPDATE users SET is_active = FALSE WHERE id = X;`
   - Bei DB-Leak-Verdacht: alle aktiven Sessions hardlöschen:
     `TRUNCATE sessions;`
   - Bei TOTP-Compromise: `TOTP_KMS_KEY` rotieren + alle TOTP-Secrets
     invalidieren (User müssen Re-Enrollen)
3. **Forensik-Triage:**
   - `admin_audit` der letzten 7 Tage exportieren — wer hat was getan?
   - `login_attempts` exportieren — Brute-Force-Muster sichtbar?

## 3. DSGVO Art. 33 — Meldung an Aufsichtsbehörde (binnen 72h)

Pflicht wenn personenbezogene Daten betroffen sind UND ein Risiko für
Betroffene besteht.

Zuständige Behörde: [zuständige Datenschutzaufsichtsbehörde].
Meldeformular: <https://www.lda.bayern.de/de/datenpanne.html>
(Bayerisches LfD — bei anderem Sitz Behörde entsprechend ersetzen).

Inhalt der Meldung (Template):

```
1. Beschreibung der Verletzung
   - Was ist passiert: ...
   - Wann wurde es entdeckt: ...
   - Wie viele Personen betroffen: ...
   - Welche Datenkategorien: [Kontodaten / Chat-Inhalte / IPs / ...]

2. Wahrscheinliche Folgen
   - ...

3. Bereits ergriffene Maßnahmen
   - ...

4. Geplante Maßnahmen
   - ...

5. Ansprechpartner
   - Datenschutzbeauftragte:r: [...]
```

Auch wenn die 72h überschritten sind: trotzdem melden, mit Begründung
der Verzögerung. Verzögerte Meldung < gar keine Meldung.

## 4. DSGVO Art. 34 — Benachrichtigung der Betroffenen

Pflicht wenn **hohes Risiko** für persönliche Rechte und Freiheiten. In
einfacher Sprache, inklusive:

- Was passiert ist
- Welche Daten betroffen
- Was die Person tun kann (Passwort ändern, 2FA aktivieren, …)
- Wer der Ansprechpartner ist

Kanal: Direkte Mail an alle betroffenen User. Falls Plattform-
Internkanal: zusätzlich Banner in der Anwendung beim nächsten Login.

## 5. Wiederherstellung

1. Root-Cause-Analyse: schreibe Postmortem mit
   - Zeitleiste (Detection → Containment → Recovery)
   - Beitragende Faktoren (technisch, prozessual)
   - Was wurde getan, was wurde nicht getan
2. Härtung: Patches, Config-Änderungen, neue Tests, neue Monitoring-
   Alerts. Jeder IR-Fall erweitert das Audit-Set.
3. Sechs-Wochen-Review: ist die Härtung tatsächlich angewandt? Sind
   die neuen Alerts feueralarm-fähig?

## 6. Jährlicher Tabletop-Test

Min. einmal pro Jahr Simulation:
- "Angreifer hat OpenRouter-API-Key gestohlen" — Eskalation
  durchspielen, ohne reale Eindämmung.
- "User meldet, dass er fremde Konversationen sieht" — Triage
  durchspielen, Klassifikation prüfen.

Ergebnis im VVT (`docs-ops/vvt.md` Versionsverlauf) eintragen.

## 7. Externe Disclosure (security.txt)

Forscher-Reports landen über `frontend/.well-known/security.txt` bei der
dort angegebenen Mailbox. SLA: erste Bestätigung binnen 72h, Triage
binnen 7 Tagen, koordinierte Disclosure-Fenster mit Reporter
abstimmen.

## 8. Nützliche Befehle (Cheatsheet)

```bash
# Live-Sessions zählen (aktive User)
docker compose exec postgres psql -U $POSTGRES_USER -d $POSTGRES_DB \
  -c "SELECT count(*) FROM sessions WHERE expires_at > now();"

# Letzte 100 Admin-Aktionen
docker compose exec postgres psql -U $POSTGRES_USER -d $POSTGRES_DB \
  -c "SELECT occurred_at, actor_id, action, target_type, target_id, ip \
      FROM admin_audit ORDER BY occurred_at DESC LIMIT 100;"

# Top fehlgeschlagene Login-Versuche der letzten Stunde
docker compose exec postgres psql -U $POSTGRES_USER -d $POSTGRES_DB \
  -c "SELECT email, ip, count(*) FROM login_attempts \
      WHERE success = FALSE AND occurred_at > now() - interval '1 hour' \
      GROUP BY email, ip ORDER BY count(*) DESC LIMIT 20;"

# SESSION_SECRET rotieren (invalidiert alle Sessions)
docker compose down
sed -i 's/^SESSION_SECRET=.*/SESSION_SECRET='"$(openssl rand -hex 32)"'/' .env
docker compose up -d
```
