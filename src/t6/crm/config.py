"""CRM Sheet settings. Only the API and the main worker read them (ADR 0003)."""
from __future__ import annotations

from collections.abc import Mapping
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
