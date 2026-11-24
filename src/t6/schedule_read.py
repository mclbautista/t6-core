"""The API's read of one Client Sheet tab: the Google credential, the merge-aware read and the extractor (#722).

Only the API process reaches this (ADR 0002, ADR 0003). The client and the CLI call the verb and never import it.
"""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from pydantic import ValidationError

from t6.schedule import extract_schedule
from t6.schedule.contract import dump_response
from t6.verbs import Done, Outcome, Refused


def read_snapshot(spreadsheet_id: str, tab_title: str, year: int | None, reference_date: date) -> Outcome:
    from t6.google.config import CREDENTIAL_VARIABLE, credential_path
    from t6.google.http import ServiceAccount, UrllibHttp
    from t6.google.sheet_tab import SheetReadError, SheetTabReader
    path = credential_path(os.environ)
    if path is None:
        return Refused(rule='config', reason=f'Set {CREDENTIAL_VARIABLE}')
    try:
        account = ServiceAccount.from_json(Path(path).read_text(encoding='utf-8'))
        request = SheetTabReader(account, UrllibHttp()).read(spreadsheet_id, tab_title, year, reference_date)
    except SheetReadError as error:
        return Refused(rule='google', reason=str(error))
    except OSError:
        return Refused(rule='config', reason='The Google credential cannot be read')
    except ValidationError:
        raise  # a request our own models reject is a bug here, not a Google or config failure
    except ValueError as error:
        return Refused(rule='config', reason=str(error))
    return Done(result=dump_response(extract_schedule(request)))
