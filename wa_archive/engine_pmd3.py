"""Backup engine that discards non-WhatsApp payloads as they arrive.

pymobiledevice3 exposes a `filter_callback` on its backup: rejected payloads are
drained from the socket but never written to disk. That is a real win — the Mac
never has to hold 60 GB — but it is worth being precise about what it does not
do. The bytes still cross the cable. From pymobiledevice3's own docstring:
"The rejected payload bytes are drained but not stored."

The reason is architectural, not an oversight. In mobilebackup2 the phone drives
the transfer: it sends DLMessageUploadFiles and pushes payloads, and the host's
only choice is whether to keep each one. The Backup request itself carries just
a MessageName and TargetIdentifier — there is no domain list to send. So:

    disk needed   ~60 GB  ->  a few hundred MB     (solved here)
    time taken    unchanged                        (not solvable host-side)
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import shutil
from pathlib import Path

from .selection import to_pymobiledevice3, whatsapp_rules


def available() -> bool:
    try:
        import pymobiledevice3  # noqa: F401
        from pymobiledevice3.services.mobilebackup2 import Mobilebackup2Service
        return hasattr(Mobilebackup2Service, "selection_filter_callback")
    except Exception:
        return False


def _build_filter(business: bool, with_media: bool):
    from pymobiledevice3.services.mobilebackup2 import Mobilebackup2Service

    rules = whatsapp_rules(business=business, with_media=with_media)
    return Mobilebackup2Service.selection_filter_callback(to_pymobiledevice3(rules))


# Below this, refusing the transfer really is the right answer.
MIN_FREE_BYTES = 2 * 1024 ** 3


def install_purge_disk_space_handler() -> bool:
    """Answer the device's purge-space request instead of aborting the backup.

    iOS sends `DLMessagePurgeDiskSpace` during the normal backup handshake: it
    is asking the host to free space if it can. pymobiledevice3 answers by
    raising `NotEnoughDiskSpaceError` unconditionally — it never reads how many
    bytes were requested, and never checks how much is actually free. So a
    backup that fits with 100 GB to spare still dies on the handshake.

    libimobiledevice's own idevicebackup2 replies "Operation not supported" and
    continues, which is what this restores. That answer is especially safe here:
    the streaming filter means the host only ever stores the WhatsApp subset, so
    the space the device is worrying about is space we were never going to use.

    We still refuse when the disk genuinely has nothing left. Returns True if
    the patch was applied.
    """
    try:
        from pymobiledevice3.services import device_link
        from pymobiledevice3.exceptions import NotEnoughDiskSpaceError
    except ImportError:
        return False

    if getattr(device_link.DeviceLink, "_wa_archive_purge_patch", False):
        return True

    async def purge_disk_space(self, message=None) -> None:
        free = shutil.disk_usage(self.root_path).free
        if free < MIN_FREE_BYTES:
            raise NotEnoughDiskSpaceError(
                f"Only {free / 1024 ** 3:.1f} GB free at {self.root_path}. "
                f"Free up space, or point --backup-dir at a bigger disk.")
        # Same reply idevicebackup2 gives: we will not purge, carry on.
        await self.status_response(-1, "Operation not supported", {})

    device_link.DeviceLink.purge_disk_space = purge_disk_space
    device_link.DeviceLink._wa_archive_purge_patch = True
    return True


async def _maybe_await(value):
    """Accept either a coroutine or a plain value.

    pymobiledevice3 turned its connection and teardown API async between major
    versions (`create_using_usbmux` and `close` are coroutines in 10.x, plain
    calls in 4.x). Rather than pin a version, tolerate both.
    """
    return await value if inspect.isawaitable(value) else value


async def _run_backup(dest: Path, passphrase: str, udid: str | None, business: bool,
                      with_media: bool, full: bool, progress) -> str:
    """Connect, back up, tear down. Returns the device udid.

    All of this has to happen inside the event loop: the udid is only populated
    once the lockdown handshake completes, so it cannot be read from the object
    before it is awaited.
    """
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.mobilebackup2 import Mobilebackup2Service

    install_purge_disk_space_handler()

    lockdown = await _maybe_await(create_using_usbmux(serial=udid))
    device_udid = lockdown.udid
    try:
        service = Mobilebackup2Service(lockdown)
        try:
            await service.backup(
                full=full,
                backup_directory=str(dest),
                progress_callback=progress,
                filter_callback=_build_filter(business, with_media),
                password=passphrase,
            )
        finally:
            with contextlib.suppress(Exception):
                await _maybe_await(service.close())
    finally:
        with contextlib.suppress(Exception):
            await _maybe_await(lockdown.close())
    return device_udid


def _connection_lost(exc: Exception) -> Exception:
    """Turn a mid-transfer disconnect into advice instead of three tracebacks.

    Nearly always this is the phone locking: iOS re-prompts for the passcode on
    the *device screen*, and if it goes unanswered the device tears down the
    session. The traceback points at asyncio internals, which tells the user
    nothing about the cause.
    """
    from .device import DeviceError

    return DeviceError(
        "The connection to the iPhone dropped mid-backup.\n\n"
        "  This is almost always the phone locking. iOS asks for the passcode\n"
        "  again ON THE PHONE'S SCREEN — never in this terminal — and ends the\n"
        "  session if it is not entered.\n\n"
        "  Before retrying:\n"
        "    1. Settings > Display & Brightness > Auto-Lock > Never\n"
        "    2. Keep the phone unlocked, plugged in, and screen on\n"
        "    3. Check the cable carries data and try a different port\n\n"
        "  Then run the same command again — the transfer resumes from where\n"
        "  it stopped rather than starting over.\n\n"
        "  If it keeps dropping, use the more robust engine:\n"
        "    wa-archive sync --engine full [--backup-dir ...]\n\n"
        f"  ({type(exc).__name__})")


def backup_whatsapp_only(dest: Path, passphrase: str, udid: str | None = None,
                         business: bool = False, with_media: bool = True,
                         full: bool = False, on_progress=None) -> Path:
    """Run a backup that only writes WhatsApp files to disk."""
    dest.mkdir(parents=True, exist_ok=True)

    def progress(pct):
        if on_progress:
            try:
                on_progress(int(float(pct)))
            except (TypeError, ValueError):
                pass

    try:
        device_udid = asyncio.run(
            _run_backup(dest, passphrase, udid, business, with_media, full, progress))
    except (ConnectionResetError, ConnectionError) as exc:
        raise _connection_lost(exc) from exc
    except Exception as exc:
        # pymobiledevice3 raises its own ConnectionTerminatedError, which does
        # not inherit from ConnectionError.
        if type(exc).__name__ in ("ConnectionTerminatedError", "ConnectionAbortedError"):
            raise _connection_lost(exc) from exc
        raise

    backup_dir = dest / device_udid
    if not (backup_dir / "Manifest.db").is_file():
        raise RuntimeError(f"Backup finished but no Manifest.db in {backup_dir}")
    return backup_dir


def explain() -> str:
    return (
        "Streaming filter: non-WhatsApp payloads are drained from the socket and\n"
        "never written. Disk stays small; transfer time is unchanged, because the\n"
        "phone decides what to send and the protocol has no domain filter."
    )
