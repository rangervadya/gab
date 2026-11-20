"""Private provider keys: existing environment or a protected bind-mounted file."""
import json
import os
from pathlib import Path
from fastapi import HTTPException

KEYS=('DADATA_API_KEY','CHECKO_API_KEY')


def api_key(name):
    if name not in KEYS:raise ValueError('Unknown integration')
    path=Path(os.environ.get('GAB_INTEGRATIONS_FILE','/srv/gab-review-data/integrations.private.json'))
    values={}
    try:
        if path.stat().st_mode & 0o077:raise HTTPException(503,'Файл ключей интеграций должен иметь права 0600')
        values=json.loads(path.read_text())
        if not isinstance(values,dict) or set(values)-set(KEYS):raise ValueError()
        if any(not isinstance(v,str) or len(v)>512 or '\n' in v or '\r' in v for v in values.values()):raise ValueError()
    except FileNotFoundError:pass
    except (OSError,ValueError):raise HTTPException(503,'Не удалось прочитать закрытые настройки интеграций') from None
    return values.get(name) or os.environ.get(name,'')


def configuration():
    return {'dadata_configured':bool(api_key('DADATA_API_KEY')),
            'checko_configured':bool(api_key('CHECKO_API_KEY'))}
