"""CRM Sheet settings. Only the API and the main worker read them (ADR 0003)."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from t6.drives.sheets import GoogleSheetsSource, SheetSource
from t6.google.config import CREDENTIAL_VARIABLE

SHEET_VARIABLE = 'T6_CRM_SHEET_ID'


def crm_source(environ: Mapping[str, str]) -> SheetSource | None:
    """The CRM Sheet, or None when `T6_CRM_SHEET_ID` is unset (every CRM feature is then off)."""
    from t6.google.http import ServiceAccount, UrllibHttp
    sheet = (environ.get(SHEET_VARIABLE) or '').strip()
    if not sheet:
        return None
    if not environ.get(CREDENTIAL_VARIABLE):
        raise ValueError(f'Set {CREDENTIAL_VARIABLE} to read the CRM Sheet')
    account = ServiceAccount.from_json(Path(environ[CREDENTIAL_VARIABLE]).read_text(encoding='utf-8'))
    return GoogleSheetsSource(sheet, account, UrllibHttp())


@dataclass(frozen=True)
class CrmRoles:
    """Telegram user IDs of the people who get quote and billing reminders (not Persons)."""
    post_producer: frozenset[int] = frozenset()
    accountant: frozenset[int] = frozenset()
    finance_head: frozenset[int] = frozenset()

    @property
    def quote(self) -> frozenset[int]:
        return self.post_producer

    @property
    def billing(self) -> frozenset[int]:
        return self.post_producer | self.accountant | self.finance_head


def _telegram_ids(environ: Mapping[str, str], name: str) -> frozenset[int]:
    """A comma list of Telegram user IDs, like `T6_CLOSER_TELEGRAM_IDS`; junk raises ValueError."""
    return frozenset(int(part) for part in (environ.get(name) or '').split(',') if part.strip())


def crm_roles(environ: Mapping[str, str]) -> CrmRoles:
    return CrmRoles(_telegram_ids(environ, 'T6_CRM_POST_PRODUCER_TELEGRAM_IDS'), _telegram_ids(environ, 'T6_CRM_ACCOUNTANT_TELEGRAM_IDS'),
                    _telegram_ids(environ, 'T6_CRM_FINANCE_HEAD_TELEGRAM_IDS'))
