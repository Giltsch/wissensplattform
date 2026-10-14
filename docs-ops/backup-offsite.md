# Off-Site-Backup Runbook

Audit-Finding C4-MED-10: `./backup.sh` schreibt nur auf das gleiche
VPS-Filesystem. Ransomware oder Host-Compromise löscht damit auch die
Backups — verstößt gegen die 3-2-1-Regel (3 Kopien, 2 Medien, 1
außerhalb).

> **Status:** Operator-Anleitung. Code-Änderung dafür nicht nötig — der
> bereits implementierte `age`-Encryption-Pfad in `backup.sh` schützt
> die Kopie auch dann, wenn das Off-Site-Ziel kompromittiert wird.

---

## Empfehlung: Hetzner Storage Box (oder Backblaze B2)

Hetzner Storage Box ist günstig (1 TB ~3 €/Monat), in Deutschland,
S3- und WebDAV-fähig. Backblaze B2 ist die billigste S3-kompatible
Alternative.

### Setup (S3-Variante, Backblaze B2)

1. Bucket `wp-backups` mit Object-Lock (Compliance-Mode, 30 Tage)
   anlegen.
2. Application-Key mit Schreibrechten **nur** auf diesem Bucket
   erzeugen.
3. Auf dem VPS `rclone` installieren und konfigurieren:

   ```bash
   apt install rclone
   rclone config  # → "n" → Name: b2 → Storage: b2 → Account+Key
   ```

4. Cron erweitern:

   ```cron
   14 3 * * * /opt/rag/backup.sh && /usr/bin/rclone copy /opt/rag/backups/ b2:wp-backups/ --include 'ragdb-*.dump.age' >> /var/log/rag-backup.log 2>&1
   ```

5. Quartalsweiser Restore-Drill (siehe unten) bleibt Pflicht.

### Setup (WebDAV-Variante, Hetzner Storage Box)

```bash
apt install davfs2
# /etc/fstab:
# https://uXXXXXX.your-storagebox.de/  /mnt/sb  davfs  rw,_netdev,user 0 0
```

Cron analog:

```cron
14 3 * * * /opt/rag/backup.sh && cp /opt/rag/backups/ragdb-*.dump.age /mnt/sb/wp/ >> /var/log/rag-backup.log 2>&1
```

## Object-Lock / Versioning ist Pflicht

Ohne Object-Lock ist Off-Site nutzlos: kompromittierter VPS-Zugang =
Angreifer kann auch die Off-Site-Kopie löschen. **30 Tage
Compliance-Mode** ist das Minimum.

## Encryption-Status

`backup.sh` schreibt seit Audit-Fix C6-HIGH-7 nur noch verschlüsselte
Dumps (`*.dump.age`). Die Off-Site-Kopie ist damit auch bei einem Leak
auf der Backup-Seite geschützt — der Angreifer bekommt nur Fernet-
verschlüsselten Datenmüll.

Der `age`-Secret-Key liegt **nicht** auf dem VPS — ausschließlich auf
dem Hardware-Token / der Operator-Workstation.

## Quartärlicher Restore-Drill

Pflicht. Ohne Drill ist die Backup-Strategie nur eine Hoffnung.

```bash
# Scratch-VPS mit gleicher Compose-Version starten
docker compose up -d postgres

# Latest backup ziehen
rclone copy b2:wp-backups/ ./restore/ --max-age 24h

# Entschlüsseln (auf Operator-Workstation, nicht auf Scratch-VPS!)
age -d -i ~/.config/age/wp.key restore/ragdb-*.dump.age > /tmp/restored.dump

# In Scratch-Compose einspielen
docker compose cp /tmp/restored.dump postgres:/tmp/
docker compose exec postgres pg_restore --clean --if-exists \
  -U "$POSTGRES_USER" -d "$POSTGRES_DB" /tmp/restored.dump

# Spot-Check: User-Login funktioniert? Konversationen sichtbar?
```

Restore-Datum + Erfolg im VVT (`docs-ops/vvt.md` Abschnitt
„Wiederherstellbarkeit") protokollieren.
