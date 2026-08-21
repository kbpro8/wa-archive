"""Which files we actually want off the phone.

pymobiledevice3 ships a `whatsapp` backup preset, but it selects only
ChatStorage.sqlite (+ -shm/-wal) — no attachments at all. It also, correctly,
covers two different domains, because where WhatsApp keeps its database moved:

  AppDomain-net.whatsapp.WhatsApp                  Documents/ChatStorage.sqlite
  AppDomainGroup-group.net.whatsapp.WhatsApp.shared  ChatStorage.sqlite

Older installs use the first; current ones use the second. A tool that only
looks at the shared group container silently finds nothing on an older device.

The rules below extend the preset with the media tree and the Business variants.
"""

from __future__ import annotations

from dataclasses import dataclass

APP_DOMAIN = "AppDomain-net.whatsapp.WhatsApp"
GROUP_DOMAIN = "AppDomainGroup-group.net.whatsapp.WhatsApp.shared"
BIZ_APP_DOMAIN = "AppDomain-net.whatsapp.WhatsAppSMB"
BIZ_GROUP_DOMAIN = "AppDomainGroup-group.net.whatsapp.WhatsAppSMB.shared"

DB_NAMES = ("ChatStorage.sqlite", "ChatStorage.sqlite-shm", "ChatStorage.sqlite-wal")
CONTACTS_NAMES = ("ContactsV2.sqlite", "ContactsV2.sqlite-shm", "ContactsV2.sqlite-wal")


@dataclass(frozen=True)
class Rule:
    """Mirrors pymobiledevice3's BackupSelectionRule so we can hand ours over."""
    domain: str
    relative_path: str
    recursive: bool = False


def whatsapp_rules(business: bool = False, with_media: bool = True,
                   with_contacts: bool = True) -> list[Rule]:
    app = BIZ_APP_DOMAIN if business else APP_DOMAIN
    group = BIZ_GROUP_DOMAIN if business else GROUP_DOMAIN

    rules: list[Rule] = []
    for name in DB_NAMES:
        rules.append(Rule(app, f"Documents/{name}"))
        rules.append(Rule(group, name))
    if with_contacts:
        for name in CONTACTS_NAMES:
            rules.append(Rule(app, f"Documents/{name}"))
            rules.append(Rule(group, name))
    if with_media:
        # Recursive: everything beneath the media tree, in both layouts.
        rules.append(Rule(group, "Message/Media", recursive=True))
        rules.append(Rule(app, "Library/Media", recursive=True))
        rules.append(Rule(app, "Documents/Media", recursive=True))
    return rules


def to_pymobiledevice3(rules: list[Rule]):
    """Convert to pymobiledevice3's own rule type."""
    from pymobiledevice3.services.mobilebackup2 import BackupSelectionRule

    return tuple(
        BackupSelectionRule(domain=r.domain, relative_path=r.relative_path,
                            recursive=r.recursive)
        for r in rules
    )


def domains(business: bool = False) -> set[str]:
    """Domains to keep when pruning a backup made by some other tool."""
    return ({BIZ_APP_DOMAIN, BIZ_GROUP_DOMAIN} if business
            else {APP_DOMAIN, GROUP_DOMAIN})
