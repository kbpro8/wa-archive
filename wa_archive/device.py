"""Drive the iPhone directly via libimobiledevice, so a sync is one click.

There is no such thing as a WhatsApp-only device backup: the mobilebackup2
protocol is driven by the phone, which decides what to send, and exposes no
per-domain filter. What we do instead is back up into our own directory, take
what we need, and delete the rest. The first run is a full backup; every run
after that is incremental and only moves what changed.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

KEYCHAIN_SERVICE = "wa-archive-backup-passphrase"
REQUIRED_TOOLS = ("idevice_id", "ideviceinfo", "idevicebackup2")
PROGRESS = re.compile(r"(\d{1,3}(?:\.\d+)?)%")


class DeviceError(RuntimeError):
    pass


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def tools_installed() -> list[str]:
    """Which of the libimobiledevice tools are missing from PATH."""
    return [t for t in REQUIRED_TOOLS if shutil.which(t) is None]


def install_hint() -> str:
    return (
        "libimobiledevice isn't installed. On macOS:\n"
        "    brew install libimobiledevice\n"
        "It talks to the iPhone over USB — no kernel extension, no daemon."
    )


@dataclass
class Device:
    udid: str
    name: str
    ios: str
    encrypted_backups: bool


def _info(udid: str, key: str, domain: str | None = None) -> str:
    cmd = ["ideviceinfo", "-u", udid]
    if domain:
        cmd += ["-q", domain]
    cmd += ["-k", key]
    r = _run(cmd)
    return r.stdout.strip() if r.returncode == 0 else ""


def connected_devices() -> list[Device]:
    missing = tools_installed()
    if missing:
        raise DeviceError(install_hint())
    r = _run(["idevice_id", "-l"])
    if r.returncode != 0:
        raise DeviceError(f"Could not list devices: {r.stderr.strip()}")

    devices = []
    for udid in filter(None, (l.strip() for l in r.stdout.splitlines())):
        will_encrypt = _info(udid, "WillEncrypt", "com.apple.mobile.backup")
        devices.append(Device(
            udid=udid,
            name=_info(udid, "DeviceName") or "iPhone",
            ios=_info(udid, "ProductVersion") or "?",
            encrypted_backups=will_encrypt.lower() == "true",
        ))
    return devices


def pick_device(udid: str | None = None) -> Device:
    devices = connected_devices()
    if not devices:
        raise DeviceError(
            "No iPhone detected. Connect it by cable, unlock it, and tap Trust.\n"
            "If it was already connected, unplug and reconnect it."
        )
    if udid:
        for d in devices:
            if d.udid == udid:
                return d
        raise DeviceError(f"No connected device with udid {udid}")
    if len(devices) > 1:
        listing = "\n".join(f"    {d.name}  {d.udid}" for d in devices)
        raise DeviceError(f"More than one device connected — pass --udid:\n{listing}")
    return devices[0]


# --------------------------------------------------------------------------
# Keychain: what makes "one click" actually one click.
# --------------------------------------------------------------------------

def keychain_get(account: str = KEYCHAIN_SERVICE) -> str | None:
    if shutil.which("security") is None:
        return None
    r = _run(["security", "find-generic-password", "-s", account, "-w"])
    return r.stdout.strip() or None if r.returncode == 0 else None


def keychain_set(passphrase: str, account: str = KEYCHAIN_SERVICE) -> bool:
    if shutil.which("security") is None:
        return False
    # -U updates in place if the item already exists.
    r = _run(["security", "add-generic-password", "-U",
              "-a", os.environ.get("USER", "wa-archive"),
              "-s", account, "-w", passphrase,
              "-D", "wa-archive backup passphrase"])
    return r.returncode == 0


def keychain_clear(account: str = KEYCHAIN_SERVICE) -> bool:
    if shutil.which("security") is None:
        return False
    return _run(["security", "delete-generic-password", "-s", account]).returncode == 0


# --------------------------------------------------------------------------
# Backup
# --------------------------------------------------------------------------

def enable_encryption(device: Device, passphrase: str) -> None:
    """Turn on backup encryption. The phone will ask for its passcode."""
    env = {**os.environ, "BACKUP_PASSWORD": passphrase}
    print("  Enabling backup encryption — confirm the passcode on your iPhone.")
    r = subprocess.run(["idevicebackup2", "-u", device.udid, "encryption", "on"],
                       env=env, text=True, capture_output=True)
    if r.returncode != 0:
        raise DeviceError(
            "Could not enable backup encryption.\n"
            f"{r.stderr.strip() or r.stdout.strip()}\n"
            "You can also enable it in Finder: select the iPhone, tick "
            "'Encrypt local backup'."
        )


def run_backup(device: Device, dest: Path, passphrase: str,
               full: bool = False, on_progress=None) -> Path:
    """Run a backup into `dest`. First run is full, later runs are incremental."""
    dest.mkdir(parents=True, exist_ok=True)
    # The passphrase goes through the environment, never argv — argv is visible
    # to every other process on the machine via `ps`.
    env = {**os.environ, "BACKUP_PASSWORD": passphrase}
    cmd = ["idevicebackup2", "-u", device.udid, "backup"]
    if full:
        cmd.append("--full")
    cmd.append(str(dest))

    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    last = -1
    tail: list[str] = []
    for line in proc.stdout:
        line = line.rstrip()
        tail = (tail + [line])[-12:]
        if "passcode" in line.lower():
            print(f"  {line}")
        m = PROGRESS.search(line)
        if m and on_progress:
            pct = int(float(m.group(1)))
            if pct != last:
                on_progress(pct)
                last = pct
    proc.wait()
    if proc.returncode != 0:
        raise DeviceError("Backup failed:\n  " + "\n  ".join(tail))

    backup_dir = dest / device.udid
    if not (backup_dir / "Manifest.db").is_file():
        raise DeviceError(f"Backup finished but produced no Manifest.db in {backup_dir}")
    return backup_dir


# --------------------------------------------------------------------------
# Pruning
# --------------------------------------------------------------------------

KEEP_FILES = {"Manifest.db", "Manifest.plist", "Info.plist", "Status.plist"}


def _manifest_file_ids(backup_dir: Path, keep_domains: set[str],
                       passphrase: str | None = None) -> set[str]:
    """fileIDs belonging to `keep_domains`, from a plain or encrypted manifest.

    In an encrypted backup Manifest.db is itself encrypted, so opening it with
    sqlite3 fails with "file is not a database". That is the normal case for
    this project — WhatsApp data is only reliably present in encrypted backups
    — so the decryption library is used whenever the direct read fails.
    """
    manifest = backup_dir / "Manifest.db"
    if not manifest.is_file():
        raise DeviceError(f"No Manifest.db in {backup_dir}; refusing to prune.")

    placeholders = ",".join("?" * len(keep_domains))
    query = f"SELECT fileID FROM Files WHERE domain IN ({placeholders})"

    try:
        conn = sqlite3.connect(f"file:{manifest}?mode=ro", uri=True)
        try:
            return {r[0] for r in conn.execute(query, tuple(keep_domains))}
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        pass                      # encrypted — fall through to the decrypter

    if not passphrase:
        raise DeviceError(
            "This backup's Manifest.db is encrypted, so pruning needs the "
            "backup passphrase. It is read from your Keychain, or set "
            "WA_BACKUP_PASSPHRASE.")
    try:
        from iphone_backup_decrypt import EncryptedBackup
    except ImportError as exc:
        raise DeviceError("Install the decryption library: "
                          "pip install iphone_backup_decrypt") from exc

    backup = EncryptedBackup(backup_directory=str(backup_dir), passphrase=passphrase)
    with backup.manifest_db_cursor() as cur:
        cur.execute(query, tuple(keep_domains))
        return {row[0] for row in cur.fetchall()}


def prune_backup(backup_dir: Path, keep_domains: set[str],
                 passphrase: str | None = None) -> tuple[int, int]:
    """Delete every backed-up file outside `keep_domains`.

    The manifest and status plists are left intact, so the next run is still
    incremental — the phone compares against its own snapshot state, not
    against what is physically on disk. The trade-off is that the backup is no
    longer restorable to a phone. That is the right trade for an archive.

    Returns (files_deleted, bytes_freed).
    """
    keep_ids = _manifest_file_ids(backup_dir, keep_domains, passphrase)

    deleted = freed = 0
    for sub in backup_dir.iterdir():
        if not sub.is_dir() or len(sub.name) != 2:
            continue
        for f in sub.iterdir():
            if f.name in keep_ids or f.name in KEEP_FILES:
                continue
            try:
                freed += f.stat().st_size
                f.unlink()
                deleted += 1
            except OSError:
                pass
        try:
            sub.rmdir()  # only succeeds if now empty
        except OSError:
            pass
    return deleted, freed


def backup_size(backup_dir: Path) -> int:
    return sum(f.stat().st_size for f in backup_dir.rglob("*") if f.is_file())
