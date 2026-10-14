# Doc-Generators (`build-*-docs.js`)

Audit-Finding C4-LOW-16: Die beiden Skripte `build-dev-docs.js` und
`build-user-docs.js` im Repo-Root erzeugen die mitgelieferten Word-
Dokumente (`Entwickler-Dokumentation.docx`, `Benutzer-Handbuch.docx`).
Sie sehen klar AI-generiert aus (~30 KB, sehr uniformes Pattern) und
laden `docx` aus einem hartcodierten Homebrew-Pfad.

> **Status:** Nur-Entwickler-Tools. Sie laufen nicht in Production
> und sind nicht Teil des deployten Container-Builds. Sie produzieren
> die `.docx`-Dateien, die getrennt zur Plattform ausgeliefert werden
> dürfen oder verworfen werden können.

## Bekannte Schwächen

1. **Kein Lockfile.** `require("docx")` resolved gegen den globalen
   Node-Module-Cache (Homebrew-Pfad). Ein kompromittiertes `docx`-Paket
   würde unbemerkt in die ausgelieferten `.docx`-Dateien einfließen.
2. **Hartcodierter macOS-Homebrew-Pfad.** Läuft nicht auf Linux-
   Entwickler-Maschinen ohne manuelles `NODE_PATH=`-Setzen.

## Wenn künftig regelmäßig verwendet

```bash
# Einmalig im Repo-Root:
npm init -y
npm install --save docx@<version>
git add package.json package-lock.json
```

Und Top-of-File ersetzen:

```javascript
// statt:  const { Document, ... } = require(path.join(process.env.NODE_PATH || ...));
const { Document, Packer, Paragraph, TextRun, HeadingLevel } = require("docx");
```

Dann:

```bash
node build-dev-docs.js
node build-user-docs.js
```

CI-Lauf: optional einen `doc-generation`-Job hinzufügen, der die
`.docx`-Outputs reproduzierbar erzeugt und gegen die committed
Versionen vergleicht. Aktuell nicht implementiert — die `.docx`-Dateien
werden bei Bedarf manuell regeneriert und committed.

## Alternative: Skripte aus dem Repo entfernen

Wenn die Word-Dokumente künftig manuell gepflegt werden oder durch
andere Tools (LibreOffice, Pandoc) erzeugt werden, sollten die
generierenden JS-Skripte ersatzlos gestrichen werden — der Wartungs-
aufwand übersteigt sonst den Nutzen.
