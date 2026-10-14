# DNS, Domain & Email-Security Runbook

Setup-Schritte außerhalb des Repos — diese müssen beim Hosting-Provider
bzw. beim Domain-Registrar gemacht werden. Belegt audit-Findings
C4-MED-13 (DMARC/SPF/CAA) und C2-LOW-6 (HSTS-Preload).

> **Status:** Operator-Anleitung. Diese Datei ist *nicht* Bestandteil
> der ausgelieferten Plattform — sie ist die Checkliste, mit der
> Domain-Einstellungen vor Go-Live durchgegangen werden.

---

## 1. CAA — Certificate-Authority-Authorization

Verhindert, dass eine kompromittierte CA gültige Zertifikate für die
Domain ausstellt. Caddy / Let's Encrypt deckt den normalen Pfad ab —
ohne CAA-Eintrag könnte aber jede beliebige CA ein Cert für die Domain
ausstellen.

```
example.com.  CAA 0 issue "letsencrypt.org"
example.com.  CAA 0 iodef "mailto:security@example.com"
```

Verifizieren nach Deploy: `dig CAA example.com +short`.

## 2. SPF — Sender Policy Framework

Die Plattform versendet aktuell **keine** Mails. Eine „SPF -all"-Policy
verhindert, dass Angreifer die Domain als Mail-Absender missbrauchen
(Phishing-Risiko, gerade weil diese Plattform ein
Mitarbeiter-internes-Tool ist und Phishing-Mails als „IT-Support"
plausibel wären).

```
example.com.  TXT  "v=spf1 -all"
```

## 3. DKIM

Solange keine Mails versendet werden: keine DKIM-Keys nötig.
Falls künftig SMTP angebunden wird (Account-Recovery o.ä.): hier dann
Selector + Public-Key des Mail-Anbieters eintragen.

## 4. DMARC — Domain-based Message Authentication

```
_dmarc.example.com.  TXT  "v=DMARC1; p=reject; sp=reject; rua=mailto:dmarc@example.com; fo=1"
```

`p=reject` = empfangende Mailserver verwerfen jede Mail, die nicht durch
SPF/DKIM autorisiert ist. Das ist die strengste und für eine
nicht-Mail-versendende Domain einzig richtige Policy.

`rua=mailto:dmarc@…` sammelt aggregierte Reports — nützlich, um
Phishing-Versuche zu sehen, sobald welche kommen. Die `dmarc@`-Mailbox
muss existieren und gelesen werden.

## 5. DNSSEC

Beim Registrar aktivieren (meist „DNSSEC: enable" — keine Operator-
Konfiguration nötig, wenn der Registrar Cloudflare/Hetzner/etc. ist).

## 6. Registrar-Lock

- Registrar-Konto mit Hardware-MFA absichern (TOTP nicht ausreichend
  für die Domain-Übertragung; YubiKey o.ä.).
- Transfer-Lock aktivieren („Domain-Transfer sperren").
- Eigene E-Mail-Adresse für Registrar-Konto sollte **nicht** auf der
  zu schützenden Domain liegen (Reverse-Dependency).

## 7. HSTS-Preload

Sobald die TLS-Konfig in `Caddyfile.example` mit
`max-age=63072000; includeSubDomains; preload` gefahren wurde und das
mindestens 30 Tage stabil läuft, Domain submitten:

- <https://hstspreload.org/>

Browser bekommen die Domain dann mit der nächsten Version vorab in den
HSTS-Cache geladen — auch der erste Besuch ist TLS-only.

## 8. Subdomain-Takeover-Prävention

Audit-Schritt vor Go-Live: wenn die Plattform unter einer Subdomain
einer größeren Domain läuft (`wissen.beispiel.de`), prüfen, ob es alte
CNAMEs auf deprovisionierte Cloud-Resources gibt. Tools wie
`OWASP Amass` oder `subjack` über die Eltern-Domain laufen lassen.

## 9. Verifikations-Befehle

Nach jedem DNS-Change:

```bash
dig +short A example.com
dig +short AAAA example.com
dig CAA example.com +short
dig TXT example.com +short
dig _dmarc.example.com TXT +short
dig +dnssec example.com | grep RRSIG  # DNSSEC-Signatur
```

Externe Audits:

- <https://www.ssllabs.com/ssltest/> (TLS-Konfig)
- <https://internet.nl/> (DNSSEC, IPv6, DANE)
- <https://hstspreload.org/?domain=example.com> (HSTS-Status)
- <https://dmarcian.com/dmarc-inspector/> (DMARC-Policy-Parser)
