"""Locate iOS backups on disk and pull WhatsApp files out of them."""

from __future__ import annotations

import os
import plistlib
import platform
import shutil
from dataclasses import dataclass
from pathlib import Path

WHATSAPP_DOMAIN = "AppDomainGroup-group.net.whatsapp.WhatsApp.shared"
WHATSAPP_BUSINESS_DOMAIN = "AppDomainGroup-group.net.whatsapp.WhatsAppSMB.shared"
CHATSTORAGE_RELPATH = "ChatStorage.sqlite"
MEDIA_RELPATH_PREFIX = "Message/Media/"

# WhatsApp has used two layouts. Current installs keep the database in the
# shared app-group container; older ones keep it inside the app's own Documents
# folder. Looking only at the group container silently finds nothing on an
# older device, so every lookup tries both, newest layout first.
LAYOUTS = {
    False: [  # personal WhatsApp
        ("AppDomainGroup-group.net.whatsapp.WhatsApp.shared", "", "Message/Media/"),
        ("AppDomain-net.whatsapp.WhatsApp", "Documents/", "Library/Media/"),
    ],
    True: [   # WhatsApp Business
        ("AppDomainGroup-group.net.whatsapp.WhatsAppSMB.shared", "", "Message/Media/"),
        ("AppDomain-net.whatsapp.WhatsAppSMB", "Documents/", "Library/Media/"),
    ],
}


def default_backup_roots() -> list[Path]:
    home = Path.home()
    system = platform.system()
    if system == "Darwin":
        return [home / "Library/Application Support/MobileSync/Backup"]
    if system == "Windows":
        return [
            home / "AppData/Roaming/Apple Computer/MobileSync/Backup",
            home / "Apple/MobileSync/Backup",
        ]
    return []


@dataclass
class BackupInfo:
    path: Path
    udid: str
    device_name: str | None
    ios_version: str | None
    last_backup: str | None
    encrypted: bool

    def describe(self) -> str:
        lock = "encrypted" if self.encrypted else "NOT encrypted"
        return (
            f"{self.device_name or '?'}  ({self.ios_version or '?'}, {lock})\n"
            f"    udid: {self.udid}\n"
            f"    last: {self.last_backup or '?'}\n"
            f"    path: {self.path}"
        )


def _read_plist(path: Path) -> dict:
    """Always hand back a dict — a truncated or odd plist must not crash a sync."""
    try:
        with path.open("rb") as fh:
            data = plistlib.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def unavailable_roots(roots: list[Path] | None = None) -> list[str]:
    """Explain backup locations that exist but can't be read right now.

    People routinely redirect MobileSync to an external disk with a symlink.
    When that disk isn't mounted the folder simply looks absent, and "no
    backups found" sends you hunting for the wrong problem — so say what is
    actually wrong instead.
    """
    problems = []
    for root in roots or default_backup_roots():
        if root.is_dir():
            continue
        if root.is_symlink():
            problems.append(
                f"{root}\n    is a symlink to {os.readlink(root)}, which isn't "
                f"available.\n    Connect that drive and try again.")
        elif root.exists():
            problems.append(f"{root}\n    exists but is not a readable folder.")
    return problems


def discover_backups(roots: list[Path] | None = None) -> list[BackupInfo]:
    """Scan the standard MobileSync locations for backup folders."""
    found: list[BackupInfo] = []
    for root in roots or default_backup_roots():
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if not (entry / "Manifest.plist").is_file():
                continue
            manifest = _read_plist(entry / "Manifest.plist")
            info = _read_plist(entry / "Info.plist")
            found.append(
                BackupInfo(
                    path=entry,
                    udid=entry.name,
                    device_name=info.get("Device Name"),
                    ios_version=info.get("Product Version"),
                    last_backup=str(info.get("Last Backup Date")) if info.get("Last Backup Date") else None,
                    encrypted=bool(manifest.get("IsEncrypted", False)),
                )
            )
    return found


class BackupSource:
    """Pulls the WhatsApp database and media tree out of a local backup.

    Encrypted backups are handled by iphone_backup_decrypt. Unencrypted ones
    are read straight from the hashed file store via Manifest.db.
    """

    def __init__(self, backup_dir: Path, passphrase: str | None = None,
                 business: bool = False):
        self.backup_dir = Path(backup_dir)
        self.business = business
        self.layouts = LAYOUTS[business]
        # Resolved on first use: whichever layout this backup actually contains.
        self.domain, self.db_prefix, self.media_prefix = self.layouts[0]
        manifest = _read_plist(self.backup_dir / "Manifest.plist")
        self.encrypted = bool(manifest.get("IsEncrypted", False))
        self._backup = None

        if self.encrypted:
            if not passphrase:
                raise ValueError("This backup is encrypted; a passphrase is required.")
            try:
                from iphone_backup_decrypt import EncryptedBackup
            except ImportError as exc:
                raise ImportError(
                    "Install the decryption library:  pip install iphone_backup_decrypt"
                ) from exc
            self._backup = EncryptedBackup(
                backup_directory=str(self.backup_dir), passphrase=passphrase
            )

    # -- unencrypted fallback ------------------------------------------------

    def detect_layout(self) -> tuple[str, str, str] | None:
        """Work out which WhatsApp layout this backup contains.

        Only usable on unencrypted backups, where Manifest.db is readable
        directly. For encrypted ones the decryption library resolves paths
        itself and we fall back to trying each layout in turn.
        """
        import sqlite3

        manifest = self.backup_dir / "Manifest.db"
        if not manifest.is_file():
            return None
        conn = sqlite3.connect(f"file:{manifest}?mode=ro", uri=True)
        try:
            for domain, db_prefix, media_prefix in self.layouts:
                hit = conn.execute(
                    "SELECT 1 FROM Files WHERE domain = ? AND relativePath = ? LIMIT 1",
                    (domain, f"{db_prefix}{CHATSTORAGE_RELPATH}"),
                ).fetchone()
                if hit:
                    self.domain, self.db_prefix, self.media_prefix = domain, db_prefix, media_prefix
                    return domain, db_prefix, media_prefix
        finally:
            conn.close()
        return None

    def _plain_file_path(self, relative_path: str, domain: str | None = None) -> Path | None:
        """Resolve domain+relativePath to its hashed location via Manifest.db."""
        import sqlite3

        conn = sqlite3.connect(f"file:{self.backup_dir / 'Manifest.db'}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT fileID FROM Files WHERE domain = ? AND relativePath = ?",
                (domain or self.domain, relative_path),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        file_id = row[0]
        return self.backup_dir / file_id[:2] / file_id

    def _plain_media_files(self) -> list[tuple[str, Path]]:
        import sqlite3

        conn = sqlite3.connect(f"file:{self.backup_dir / 'Manifest.db'}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT relativePath, fileID FROM Files "
                "WHERE domain = ? AND relativePath LIKE ? AND flags = 1",
                (self.domain, self.media_prefix + "%"),
            ).fetchall()
        finally:
            conn.close()
        return [(rp, self.backup_dir / fid[:2] / fid) for rp, fid in rows]

    # -- public API ----------------------------------------------------------

    def extract_chatstorage(self, output: Path) -> Path:
        output.parent.mkdir(parents=True, exist_ok=True)
        if self._backup:
            from iphone_backup_decrypt import RelativePath

            self._backup.extract_file(
                relative_path=RelativePath.WHATSAPP_MESSAGES,
                output_filename=str(output),
            )
        else:
            self.detect_layout()
            src = self._plain_file_path(f"{self.db_prefix}{CHATSTORAGE_RELPATH}")
            if not src or not src.is_file():
                raise FileNotFoundError(
                    "ChatStorage.sqlite not found in this backup. If the backup is "
                    "unencrypted, re-run it with encryption enabled — WhatsApp data "
                    "is far more reliably included."
                )
            shutil.copy2(src, output)
        if not output.is_file() or output.stat().st_size == 0:
            raise FileNotFoundError(f"Extraction produced no usable file at {output}")
        return output

    def extract_media(self, staging_dir: Path) -> Path:
        """Bulk-extract Message/Media/** preserving folder structure.

        Folder structure matters: ZMEDIALOCALPATH in ChatStorage is relative to
        the WhatsApp container, so staging/Message/<ZMEDIALOCALPATH> is the file.
        """
        staging_dir.mkdir(parents=True, exist_ok=True)
        if self._backup:
            from iphone_backup_decrypt import MatchFiles

            self._backup.extract_files(
                **MatchFiles.WHATSAPP_ATTACHMENTS,
                output_folder=str(staging_dir),
                preserve_folders=True,
            )
        else:
            for relative_path, src in self._plain_media_files():
                if not src.is_file():
                    continue
                dest = staging_dir / relative_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
        return staging_dir
