/* Generate Benutzer-Handbuch.docx for end-users of the Wissensplattform.
 * White-label: the product name and accent colour are neutral defaults and
 * can be overridden at build time via BRAND_APP_NAME / BRAND_SUB_BRAND /
 * BRAND_ACCENT (hex without leading #). The admin Branding tab is the
 * runtime source of truth; these only style the generated handbook.
 * Run: node build-user-docs.js
 */
const fs = require("fs");
const path = require("path");
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  Header, Footer, AlignmentType, LevelFormat, HeadingLevel,
  BorderStyle, WidthType, ShadingType, PageNumber, PageBreak,
  TableOfContents,
} = require(path.join(process.env.NODE_PATH || "/opt/homebrew/lib/node_modules", "docx"));

const ARIAL = "Arial";
const MONO = "Menlo";
const APP_NAME = process.env.BRAND_APP_NAME || "Wissensplattform";
const SUB_BRAND = process.env.BRAND_SUB_BRAND || "";
const PRODUCT = SUB_BRAND ? `${APP_NAME} ${SUB_BRAND}` : APP_NAME;
const ACCENT = process.env.BRAND_ACCENT || "2563EB";
const FG = "000000";

const border = { style: BorderStyle.SINGLE, size: 4, color: "CCCCCC" };
const cellBorders = { top: border, bottom: border, left: border, right: border };

const h1 = (t) => new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun(t)] });
const h2 = (t) => new Paragraph({ heading: HeadingLevel.HEADING_2, children: [new TextRun(t)] });

const p = (text, opts = {}) => new Paragraph({
  children: [new TextRun({ text, ...opts })],
  spacing: { after: 120 },
});

const tip = (text) => new Paragraph({
  children: [new TextRun({ text: "💡  " + text, italics: true, color: "555555" })],
  shading: { fill: "F8F8F0", type: ShadingType.CLEAR },
  border: { left: { style: BorderStyle.SINGLE, size: 12, color: ACCENT, space: 6 } },
  spacing: { before: 120, after: 120 },
});

const warn = (text) => new Paragraph({
  children: [new TextRun({ text: "⚠️  " + text, italics: true, color: "555555" })],
  shading: { fill: "FFF6F0", type: ShadingType.CLEAR },
  border: { left: { style: BorderStyle.SINGLE, size: 12, color: "D97706", space: 6 } },
  spacing: { before: 120, after: 120 },
});

const code = (text) => new Paragraph({
  children: [new TextRun({ text, font: MONO, size: 20 })],
  shading: { fill: "F5F5F5", type: ShadingType.CLEAR },
  spacing: { before: 80, after: 80 },
});

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

function tbl(headers, rows) {
  const colWidths = headers.map(() => Math.floor(9000 / headers.length));
  colWidths[colWidths.length - 1] += 9000 - colWidths.reduce((a,b)=>a+b, 0);
  const mkCell = (text, isHeader, idx) => new TableCell({
    borders: cellBorders,
    width: { size: colWidths[idx], type: WidthType.DXA },
    shading: isHeader ? { fill: "EFEFEF", type: ShadingType.CLEAR } : undefined,
    margins: { top: 80, bottom: 80, left: 120, right: 120 },
    children: [new Paragraph({ children: [new TextRun({ text: String(text), bold: isHeader })] })],
  });
  return new Table({
    width: { size: 9000, type: WidthType.DXA },
    columnWidths: colWidths,
    rows: [
      new TableRow({ children: headers.map((c, i) => mkCell(c, true, i)) }),
      ...rows.map(r => new TableRow({ children: r.map((c, i) => mkCell(c, false, i)) })),
    ],
  });
}

const blank = () => new Paragraph({ children: [new TextRun("")] });

// ---- Cover ----
const cover = [
  new Paragraph({
    children: [new TextRun({ text: PRODUCT, size: 56, bold: true })],
    spacing: { before: 2400, after: 240 },
  }),
  new Paragraph({
    children: [new TextRun({ text: "Benutzer-Handbuch", size: 36, color: "666666" })],
    spacing: { after: 240 },
  }),
  new Paragraph({
    children: [new TextRun({ text: "Wie Sie schnell Antworten aus Ihren internen Dokumenten erhalten.", size: 22, italics: true, color: "666666" })],
    spacing: { after: 1200 },
  }),
  new Paragraph({
    children: [new TextRun({ text: `Stand: ${new Date().toISOString().slice(0,10)}`, size: 20, color: "888888" })],
  }),
  new Paragraph({ children: [new PageBreak()] }),
];

// ---- TOC ----
const toc = [
  h1("Inhaltsverzeichnis"),
  new TableOfContents("Inhalt", { hyperlink: true, headingStyleRange: "1-2" }),
  new Paragraph({ children: [new PageBreak()] }),
];

const content = [];

// 1. Was ist das?
content.push(h1(`1. Was ist ${PRODUCT}?`));
content.push(p(`${PRODUCT} ist eine intelligente Suche für Ihre internen Firmendokumente. Sie stellen Fragen in normalem Deutsch — die Anwendung findet die relevanten Stellen in den hochgeladenen Dokumenten und formuliert eine kurze Antwort mit Quellenangabe.`));
content.push(blank());
content.push(p("Was die Plattform NICHT ist: keine Web-Suche, kein allgemeiner Chatbot, kein Ersatz für Fachberatung. Antworten kommen ausschließlich aus den Dokumenten, die Ihr Unternehmen bereitgestellt hat."));
content.push(blank());
content.push(tip("Wenn Sie eine Frage stellen, die nicht durch ein hinterlegtes Dokument beantwortbar ist, antwortet das System ehrlich „Ich kann das anhand der verfügbaren Dokumente nicht beantworten\" — anstatt zu raten."));

content.push(new Paragraph({ children: [new PageBreak()] }));

// 2. Anmelden
content.push(h1("2. Anmelden"));
content.push(p("Sie haben von Ihrer Administratorin oder Ihrem Administrator eine E-Mail-Adresse und ein Passwort erhalten. Eine Selbstregistrierung ist nicht möglich."));
content.push(blank());
content.push(h2("2.1 Schritt für Schritt"));
content.push(numbered("Öffnen Sie die Anwendung in Ihrem Browser (URL bekommen Sie von Ihrer Administration)."));
content.push(numbered("Tragen Sie Ihre E-Mail-Adresse und Ihr Passwort ein."));
content.push(numbered("Klick auf „Anmelden\". Sie landen direkt im Chat."));
content.push(blank());
content.push(h2("2.2 Wenn die Anmeldung nicht klappt"));
content.push(bullet("„E-Mail oder Passwort ist ungültig\" — beide Werte prüfen, Caps-Lock?"));
content.push(bullet("„Zu viele Versuche, bitte in einer Minute erneut versuchen\" — kurz warten, dann erneut. Nach 10 fehlgeschlagenen Versuchen ist das Konto 15 Minuten gesperrt."));
content.push(bullet("Passwort vergessen? Wenden Sie sich an Ihre Administration. Es gibt keinen Self-Service-Reset (bewusste Designentscheidung für interne Tools)."));
content.push(blank());
content.push(warn("Behandeln Sie Ihr Passwort wie jedes andere Geschäftspasswort. Niemand fragt es per E-Mail oder Telefon ab."));

content.push(new Paragraph({ children: [new PageBreak()] }));

// 3. Fragen stellen
content.push(h1("3. Fragen stellen"));
content.push(p("Im Chat-Eingabefeld unten können Sie Fragen in vollständigen Sätzen formulieren. Drücken Sie Enter zum Senden, Shift+Enter für eine neue Zeile."));
content.push(blank());
content.push(h2("3.1 Was funktioniert gut"));
content.push(bullet("Konkrete Sachfragen: „Wie hoch ist die Tagespauschale für Verpflegung?\""));
content.push(bullet("„Wie lange ist der Aufbewahrungsfrist von Bestellunterlagen?\""));
content.push(bullet("„Welche Schritte muss ich tun, um Zugang zum VPN zu bekommen?\""));
content.push(bullet("Anschlussfragen ohne Wiederholung: nach „Was sind die Tier-1-SLA?\" einfach „Und Tier-2?\""));
content.push(blank());
content.push(h2("3.2 Was schwierig ist"));
content.push(bullet("Sehr offene Fragen wie „Was sollte ich heute tun?\" — die Plattform kennt nur Ihre Dokumente, nicht Ihren Kalender."));
content.push(bullet("Themen, die in keinem hinterlegten Dokument erwähnt werden — die Plattform sagt dann ehrlich, dass sie es nicht weiß."));
content.push(bullet("Rechenfragen oder mathematische Probleme — das ist nicht der Zweck."));
content.push(blank());
content.push(tip("Die Anwendung versteht Anschlussfragen. Sie müssen das Thema nicht jedes Mal wiederholen — fragen Sie einfach weiter wie in einem normalen Gespräch."));

content.push(new Paragraph({ children: [new PageBreak()] }));

// 4. Antworten verstehen
content.push(h1("4. Antworten verstehen"));
content.push(p("Jede Antwort enthält in eckigen Klammern die Datei, aus der die Information stammt — z. B. „[handbuch.pdf]\". So können Sie die Quelle nachvollziehen."));
content.push(blank());
content.push(h2("4.1 Quellen-Pills"));
content.push(p("Unter jeder Antwort sehen Sie kleine „Pills\" mit den Dateinamen der herangezogenen Dokumente. Das sind die Top-5 Treffer, die das System für Ihre Frage als am relevantesten bewertet hat."));
content.push(blank());
content.push(h2("4.1a Inline-Zitationen im Antworttext"));
content.push(p("Innerhalb der Antwort selbst werden Quellenangaben wie „[handbuch.pdf]\" als kleine grüne Marker direkt im Text dargestellt. So sehen Sie sofort, welcher Satz auf welches Dokument zurückgeht — ohne nach unten zu scrollen."));
content.push(blank());
content.push(h2("4.2 Quelltext anzeigen"));
content.push(p("Klick auf „Quelltext anzeigen\" öffnet die genauen Textstellen, die das System gelesen hat. So können Sie selbst prüfen, ob die Antwort korrekt aus dem Dokument abgeleitet wurde."));
content.push(blank());
content.push(h2("4.3 Volltext öffnen (Doc-Viewer)"));
content.push(p("Sowohl die Quellen-Pills unter der Antwort als auch die Snippet-Header im Quelltext-Block sind klickbar. Ein Klick öffnet das gesamte Dokument in einem Overlay; der zitierte Abschnitt ist farblich hervorgehoben und automatisch eingescrollt."));
content.push(blank());
content.push(tip("Schließen mit Esc, Klick auf den Hintergrund oder das ×. So können Sie schnell den Kontext um eine Aussage herum lesen, ohne die Unterhaltung zu verlassen."));
content.push(blank());
content.push(warn("Falls die Antwort von dem abweicht, was im Quelltext steht: vertrauen Sie dem Quelltext, nicht der Zusammenfassung. Bei wichtigen Entscheidungen immer das Originaldokument konsultieren."));

content.push(new Paragraph({ children: [new PageBreak()] }));

// 2b. Zwei-Faktor (Konto)
content.push(h1("2a. Zwei-Faktor-Authentifizierung (optional)"));
content.push(p("Wir empfehlen, vor allem für Admin-Konten, einen zweiten Faktor zu aktivieren. Sie brauchen dazu eine Authenticator-App auf dem Smartphone (1Password, Aegis, Google Authenticator, …)."));
content.push(blank());
content.push(h2("Aktivieren"));
content.push(numbered("Oben rechts auf „Konto\" klicken. Im Dialog erscheint der Bereich „Zwei-Faktor-Authentifizierung\"."));
content.push(numbered("Auf „Aktivieren\" klicken. Es werden ein QR-Code und ein Geheimnis angezeigt."));
content.push(numbered("In Ihrer Authenticator-App: QR-Code scannen ODER das Geheimnis manuell eingeben."));
content.push(numbered("Den aktuellen 6-stelligen Code aus der App ins Eingabefeld eintragen → „Aktivieren\". Sobald der Code passt, ist 2FA aktiv."));
content.push(blank());
content.push(h2("Bei der nächsten Anmeldung"));
content.push(p("Sie geben wie gewohnt E-Mail und Passwort ein. Anschließend erscheint ein zweites Feld für den 6-stelligen Code aus Ihrer App."));
content.push(blank());
content.push(h2("Deaktivieren"));
content.push(p("Im selben Dialog. Sie müssen einen aktuellen Code eingeben, damit niemand mit gestohlener Sitzung 2FA still entfernen kann."));
content.push(blank());
content.push(warn("Authenticator-Gerät verloren? Wenden Sie sich an die Administration. Beim Passwort-Reset durch eine Admin-Person wird auch 2FA automatisch entfernt — Sie können sich dann mit dem neuen Passwort einloggen und 2FA neu einrichten."));

content.push(new Paragraph({ children: [new PageBreak()] }));

// 4b. Feedback geben
content.push(h1("5. Feedback geben"));
content.push(p("Unter jeder Antwort des Systems sehen Sie zwei kleine Buttons: 👍 (hilfreich) und 👎 (nicht hilfreich). Ein Klick speichert Ihre Bewertung sofort."));
content.push(blank());
content.push(h2("5.1 Wie funktioniert das?"));
content.push(numbered("Klick auf 👍 — die Bewertung wird gespeichert."));
content.push(numbered("Klick auf 👎 — Sie können optional kurz erklären, was nicht passte (Eingabefeld erscheint). Leer lassen ist ok."));
content.push(numbered("Klick erneut auf den aktiven Button entfernt Ihre Bewertung wieder."));
content.push(blank());
content.push(h2("5.2 Was passiert mit dem Feedback?"));
content.push(bullet("Bewertungen sind Ihrem Konto zugeordnet, andere Nutzer sehen sie nicht."));
content.push(bullet("Administratoren können aggregierte Statistiken sehen (z. B. Anzahl 👍 vs. 👎), aber NICHT, welche einzelne Antwort Sie wie bewertet haben."));
content.push(bullet("Daumen-runter mit Kommentar hilft uns, schwache Antworten zu finden — z. B. „Quelle stammt aus veraltetem Dokument\" oder „Frage falsch verstanden\"."));
content.push(blank());
content.push(tip("Feedback ist der einfachste Weg, die Qualität der Plattform zu verbessern. Wenn eine Antwort offensichtlich falsch oder unvollständig ist: kurz 👎 und (optional) eine Zeile erklärt warum."));

content.push(new Paragraph({ children: [new PageBreak()] }));

// 5. Unterhaltungen
content.push(h1("6. Unterhaltungen verwalten"));
content.push(p("Auf der linken Seite sehen Sie eine Liste Ihrer Unterhaltungen. Jede Frage-Antwort-Kette ist eine eigenständige Unterhaltung."));
content.push(blank());
content.push(h2("6.1 Neue Unterhaltung starten"));
content.push(p("Oben links auf „Neue Unterhaltung\" klicken. Der Chat-Bereich wird geleert; das System erstellt automatisch einen Eintrag, sobald Sie Ihre erste Frage senden."));
content.push(blank());
content.push(h2("6.2 Frühere Unterhaltung öffnen"));
content.push(p("Klick auf einen Eintrag in der Sidebar lädt die komplette Unterhaltung zurück in den Chat. Sie können dort weiterführen, als wären Sie nie weg gewesen."));
content.push(blank());
content.push(h2("6.3 Umbenennen"));
content.push(p("Beim Hover über einen Eintrag erscheint rechts ein „⋯\"-Menü. Dort wählen Sie „Umbenennen\". Standardmäßig setzt das System den Titel aus der ersten Frage."));
content.push(blank());
content.push(h2("6.4 Löschen"));
content.push(p("Im selben „⋯\"-Menü liegt „Löschen\". Eine gelöschte Unterhaltung samt aller Nachrichten ist endgültig weg — Wiederherstellung ist nicht möglich."));
content.push(blank());
content.push(h2("6.5 Mobile-Ansicht"));
content.push(p("Auf dem Smartphone wird die Sidebar zur einklappbaren Schublade. Über das ☰-Symbol oben links öffnen Sie sie."));

content.push(new Paragraph({ children: [new PageBreak()] }));

// 6. Tageslimit
content.push(h1("7. Tageslimit & Privatsphäre"));
content.push(h2("7.1 Wie viele Fragen pro Tag?"));
content.push(p("Pro Benutzerkonto sind standardmäßig 200 Fragen pro Kalendertag erlaubt. Das ist großzügig bemessen — ein typischer Arbeitstag liegt deutlich darunter. Wenn Sie das Limit erreichen, erhalten Sie einen Hinweis und können am nächsten Tag wieder loslegen."));
content.push(blank());
content.push(h2("7.2 Was wird gespeichert?"));
content.push(bullet("Ihre Anmeldedaten (E-Mail, gehashtes Passwort, Aktiv-Status)"));
content.push(bullet("Ihre Unterhaltungen (Fragen, Antworten, Quellenangaben) — damit Sie sie später wieder öffnen können"));
content.push(bullet("Anmeldeversuche (E-Mail, IP, Zeitpunkt) — zur Erkennung von Brute-Force-Angriffen"));
content.push(bullet("Anzahl Ihrer täglichen Anfragen (für das Tageslimit)"));
content.push(blank());
content.push(h2("7.3 Was wird NICHT gespeichert?"));
content.push(bullet("Inhalte Ihrer Unterhaltungen erscheinen NICHT in den Server-Protokolldateien"));
content.push(bullet("Keine Tracking- oder Marketing-Cookies — nur ein einzelner Sitzungs-Cookie für die Anmeldung"));
content.push(blank());
content.push(h2("7.4 Wer sieht meine Unterhaltungen?"));
content.push(p("Ihre Unterhaltungen sind ausschließlich Ihrem eigenen Benutzerkonto zugeordnet. Selbst Administratoren haben über die Oberfläche KEINEN Zugriff darauf. Beim Löschen Ihres Kontos werden alle zugehörigen Unterhaltungen automatisch entfernt."));
content.push(blank());
content.push(tip("Sie können einzelne Unterhaltungen jederzeit selbst löschen — das ist Ihr DSGVO-Recht (Art. 17, Recht auf Vergessenwerden)."));

content.push(new Paragraph({ children: [new PageBreak()] }));

// 7. Für Admins: Dokumente
content.push(h1("8. Für Administratoren: Dokumente"));
content.push(p("Als Administrator sehen Sie oben rechts den Button „Admin\". Klick öffnet den Verwaltungsbereich mit drei Tabs: Dokumente · Benutzer · Branding."));
content.push(blank());
content.push(h2("8.1 Neues Dokument hochladen"));
content.push(numbered("Tab „Dokumente\" wählen."));
content.push(numbered("Datei auswählen — erlaubt sind .pdf, .md, .markdown, .txt."));
content.push(numbered("Auf „Hochladen und einlesen\" klicken."));
content.push(numbered("Das System verarbeitet die Datei (kann je nach Größe ein paar Sekunden dauern). Bei Erfolg erscheint die Bestätigung mit Anzahl der erstellten Abschnitte."));
content.push(blank());
content.push(h2("8.2 Was passiert beim Upload?"));
content.push(bullet("Die Datei wird auf dem Server gespeichert (nur intern erreichbar)."));
content.push(bullet("Der Inhalt wird in Abschnitte (~380 Wörter) zerlegt."));
content.push(bullet("Jeder Abschnitt wird mit einem KI-Modell als Vektor gespeichert (für die spätere semantische Suche)."));
content.push(bullet("Ab dem Moment kann das System auf das Dokument zugreifen — keine Verzögerung."));
content.push(blank());
content.push(h2("8.3 Aktualisierte Version hochladen (Doc-Versioning)"));
content.push(p("Wenn Sie eine Datei mit dem gleichen Dateinamen wie ein bereits vorhandenes Dokument hochladen, ersetzt das System die alte Version automatisch — die alte wandert ins Archiv (für die Suche nicht mehr sichtbar), die neue wird zur „v2\". In der Dokumentenliste sehen Sie das Versionskürzel rechts neben dem Namen sowie die Anzahl der archivierten Vorgängerversionen."));
content.push(blank());
content.push(tip("Identische Inhalte werden weiterhin abgelehnt. Wenn Sie also wirklich eine inhaltliche Änderung haben, akzeptiert das System sie als neue Version. Wenn Sie aus Versehen die gleiche Datei nochmal hochladen, sehen Sie eine 409-Meldung — kein Schaden."));
content.push(blank());
content.push(h2("8.4 Dokument löschen"));
content.push(p("In der Liste „Vorhandene Dokumente\" steht hinter jedem Eintrag ein „Löschen\"-Button. Beim Löschen werden sowohl die Datei als auch alle daraus erzeugten Abschnitte vollständig entfernt."));
content.push(blank());
content.push(h2("8.5 Größenbegrenzungen"));
content.push(table_(
  [
    ["Maximale Dateigröße", "20 MB"],
    ["Erlaubte Dateiendungen", ".pdf, .md, .markdown, .txt"],
    ["Erlaubte Zeichen im Dateinamen", "A–Z, a–z, 0–9, ., -, _"],
  ]
));
content.push(blank());
content.push(warn("Hochgeladene Dokumente sind sofort für ALLE eingeloggten Benutzer zugänglich. Achten Sie darauf, was Sie hochladen — keine personenbezogenen Daten ohne Rechtsgrundlage, keine vertraulichen Informationen ohne Notwendigkeit."));

content.push(new Paragraph({ children: [new PageBreak()] }));

// 8. Für Admins: Benutzer
content.push(h1("9. Für Administratoren: Benutzer"));
content.push(h2("9.1 Neuen Benutzer anlegen"));
content.push(numbered("Tab „Benutzer\" → „Neuen Benutzer anlegen\"."));
content.push(numbered("E-Mail-Adresse eingeben, Passwort (mindestens 12 Zeichen) festlegen."));
content.push(numbered("Bei Bedarf „Admin-Rechte\" anhaken."));
content.push(numbered("Auf „Anlegen\" klicken."));
content.push(blank());
content.push(tip("Geben Sie das initiale Passwort über einen sicheren Kanal weiter (Passwortmanager, persönlich, verschlüsselte Nachricht) — niemals per unverschlüsselter E-Mail."));
content.push(blank());
content.push(h2("9.2 Bestehende Benutzer verwalten"));
content.push(p("Pro Benutzer stehen vier Aktionen zur Verfügung:"));
content.push(bullet("„Zum Admin machen / Admin entziehen\" — Rolle umschalten."));
content.push(bullet("„Deaktivieren / Aktivieren\" — Konto temporär sperren ohne Daten zu verlieren. Aktive Sitzungen werden beim Deaktivieren beendet."));
content.push(bullet("„Passwort zurücksetzen\" — neues Passwort vergeben. Bestehende Sitzungen werden invalidiert; der Nutzer muss sich neu anmelden."));
content.push(bullet("„Löschen\" — endgültiges Entfernen des Kontos. Alle Unterhaltungen des Nutzers werden mitgelöscht."));
content.push(blank());
content.push(warn("Sie können sich selbst nicht löschen, deaktivieren oder die Admin-Rechte entziehen. Das schützt vor versehentlichem Lock-out aus der Admin-Oberfläche."));

content.push(new Paragraph({ children: [new PageBreak()] }));

// 8b. Analytics
content.push(h1("10. Für Administratoren: Analytics"));
content.push(p("Im Tab „Analytics\" finden Sie eine Übersicht über die Nutzung und die Antwortqualität der Plattform — alles auf einer Seite, ohne SQL-Kenntnisse."));
content.push(blank());
content.push(h2("10.1 Auf einen Blick"));
content.push(bullet("Anzahl Benutzer, Dokumente, Unterhaltungen und Nachrichten gesamt"));
content.push(bullet("Aktivität der letzten 7 und 30 Tage (Unterhaltungen, Nachrichten, aktive Benutzer)"));
content.push(blank());
content.push(h2("10.2 Feedback-Verteilung"));
content.push(p("Wie viele Antworten wurden mit 👍 oder 👎 bewertet, wie viele blieben unbewertet, wie oft wurde ein Kommentar hinterlassen. Hilft Ihnen, problematische Antwortmuster zu erkennen."));
content.push(blank());
content.push(h2("10.3 Refusal-Quote"));
content.push(p("Prozentsatz der Anfragen in den letzten 30 Tagen, bei denen das System mit „Ich kann das anhand der verfügbaren Dokumente nicht beantworten\" geantwortet hat. Eine hohe Quote ist nicht per se schlecht — aber wenn Mitarbeitende oft Fragen stellen, die nicht beantwortet werden können, fehlen vielleicht Dokumente im Korpus."));
content.push(blank());
content.push(h2("10.4 Meistzitierte Dokumente"));
content.push(p("Welche Dokumente werden am häufigsten als Quelle herangezogen — eine schnelle Sicht darauf, welche Inhalte in der Praxis nachgefragt werden. Dokumente, die nie auftauchen, sind Kandidaten für eine Aussortierung oder bessere Pflege."));
content.push(blank());
content.push(h2("10.5 Tägliche Chat-Anfragen"));
content.push(p("Balkendiagramm der letzten 30 Tage. Hover über einen Balken zeigt das Datum und die exakte Anzahl. Spitzen können auf neue Onboardings oder Themen-Wellen hinweisen."));
content.push(blank());
content.push(h2("10.6 Anmeldungen"));
content.push(p("Erfolgreiche und fehlgeschlagene Login-Versuche der letzten 7 Tage. Auffällig viele Fehlversuche können ein Hinweis auf Brute-Force-Versuche sein — die Plattform sperrt Accounts dann automatisch nach 10 Versuchen für 15 Minuten."));

content.push(new Paragraph({ children: [new PageBreak()] }));

// 9. Branding
content.push(h1("11. Für Administratoren: Markenanpassung"));
content.push(p("Im Tab „Branding\" passen Sie das Erscheinungsbild der gesamten Anwendung an Ihre Marke an."));
content.push(blank());
content.push(h2("11.1 Was lässt sich ändern?"));
content.push(table_(
  [
    ["App-Name", "Der Hauptname der Anwendung"],
    ["Sub-Brand", "Der fettgedruckte Zusatz (optional)"],
    ["Akzentfarbe", "Die signalfarbene Hervorhebung (z. B. Lime-Grün)"],
    ["Tagline", "Optionaler Untertitel"],
  ]
));
content.push(blank());
content.push(h2("11.2 Wo wird das angezeigt?"));
content.push(bullet("Login-Seite (auch für nicht eingeloggte Besucher)"));
content.push(bullet("Wortmarke oben in jeder Ansicht"));
content.push(bullet("Browser-Tab-Titel"));
content.push(bullet("Akzentfarbe in Buttons, Quellen-Hervorhebungen und Sidebar"));
content.push(blank());
content.push(p("Änderungen sind sofort wirksam — sowohl Ihre eigene Ansicht als auch die anderer eingeloggter Benutzer beim nächsten Reload."));

content.push(new Paragraph({ children: [new PageBreak()] }));

// 10. FAQ
content.push(h1("12. Häufige Fragen"));
content.push(blank());
content.push(p("Warum kann ich keine Datei hochladen?", { bold: true }));
content.push(p("Nur Administrator-Konten haben Zugriff auf den Upload. Bitten Sie Ihre Administration, die Datei für Sie hochzuladen — oder Ihnen Admin-Rechte zu geben."));
content.push(blank());
content.push(p("Warum sagt das System manchmal „kann ich nicht beantworten\"?", { bold: true }));
content.push(p("Das ist eine Sicherheitsfunktion: Wenn das System keine genug relevante Stelle in den Dokumenten findet, weigert es sich zu antworten — anstatt zu raten oder zu erfinden. Das ist gewollt: Lieber kein Antwort als eine falsche."));
content.push(blank());
content.push(p("Können andere Benutzer meine Fragen sehen?", { bold: true }));
content.push(p("Nein. Ihre Unterhaltungen sind privat und nur Ihrem Konto zugeordnet. Sogar Administratoren haben über die Oberfläche keinen Zugriff."));
content.push(blank());
content.push(p("Werden meine Fragen an externe Dienste geschickt?", { bold: true }));
content.push(p("Ja — die Frage zusammen mit den passenden Dokument-Auszügen wird an OpenRouter (KI-Routing-Anbieter) und je nach Modell an US-Anbieter wie OpenAI oder Anthropic übermittelt. Details stehen in der Datenschutzerklärung. Senden Sie keine personenbezogenen Daten oder vertraulichen Informationen, wenn das nicht zwingend nötig ist."));
content.push(blank());
content.push(p("Ich sehe ein Dokument in der Liste, aber es wird nie zitiert. Warum?", { bold: true }));
content.push(p("Möglich, dass keine Ihrer Fragen thematisch zu diesem Dokument passt — das System zitiert nur, was relevant ist. Oder das Dokument enthält nach dem Einlesen wenig durchsuchbaren Text (z. B. PDFs mit eingescannten Bildern ohne OCR)."));
content.push(blank());
content.push(p("Wie kann ich Fehler oder schlechte Antworten melden?", { bold: true }));
content.push(p("Unter jeder Antwort sind 👍/👎 Buttons. Klicken Sie auf 👎 und tragen optional einen kurzen Hinweis ein („zu vage\", „zitiert veraltetes Dokument\" usw.). Administratoren sehen die Quote der negativen Bewertungen aggregiert im Analytics-Dashboard und können daraus Verbesserungen ableiten."));

content.push(new Paragraph({ children: [new PageBreak()] }));

// 11. Tastenkombinationen
content.push(h1("13. Tastenkombinationen"));
content.push(table_(
  [
    ["Enter", "Frage senden"],
    ["Shift + Enter", "Neue Zeile im Eingabefeld"],
    ["Esc", "Sidebar/Menü schließen (auf Mobile: Drawer schließen)"],
  ]
));

// helper for two-column tables
function table_(rows) {
  const colWidths = [3000, 6000];
  const mkCell = (text, isHeader, idx) => new TableCell({
    borders: cellBorders,
    width: { size: colWidths[idx], type: WidthType.DXA },
    shading: isHeader ? { fill: "EFEFEF", type: ShadingType.CLEAR } : undefined,
    margins: { top: 80, bottom: 80, left: 120, right: 120 },
    children: [new Paragraph({ children: [new TextRun({ text: String(text), bold: isHeader || idx === 0 })] })],
  });
  return new Table({
    width: { size: 9000, type: WidthType.DXA },
    columnWidths: colWidths,
    rows: rows.map(r => new TableRow({ children: r.map((c, i) => mkCell(c, false, i)) })),
  });
}

const finalContent = [...cover, ...toc, ...content];

const doc = new Document({
  creator: "wissenplattform",
  title: `${PRODUCT} — Benutzer-Handbuch`,
  styles: {
    default: { document: { run: { font: ARIAL, size: 22 } } },
    paragraphStyles: [
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 36, bold: true, font: ARIAL, color: FG },
        paragraph: { spacing: { before: 360, after: 200 }, outlineLevel: 0 } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 28, bold: true, font: ARIAL, color: FG },
        paragraph: { spacing: { before: 240, after: 140 }, outlineLevel: 1 } },
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
        size: { width: 11906, height: 16838 },
        margin: { top: 1440, right: 1440, bottom: 1440, left: 1440 },
      },
    },
    headers: {
      default: new Header({ children: [new Paragraph({
        alignment: AlignmentType.RIGHT,
        children: [new TextRun({ text: `${PRODUCT} — Benutzer-Handbuch`, color: "888888", size: 18 })],
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
  fs.writeFileSync("Benutzer-Handbuch.docx", buf);
  console.log("ok: Benutzer-Handbuch.docx (" + buf.length + " bytes)");
});
