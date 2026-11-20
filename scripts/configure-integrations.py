#!/usr/bin/env python3
"""Set existing DaData/Checko keys privately on the server, without restarting CRM."""
import getpass
import json
import os
from pathlib import Path
import subprocess
import tempfile


def write_private(path,values,uid,gid):
    path=Path(path)
    if path.resolve().is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError('Ключи нельзя сохранять в репозитории')
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.integrations-',dir=path.parent)
    try:
        os.fchmod(fd,0o600)
        os.fchown(fd,uid,gid)
        with os.fdopen(fd,'w') as stream:
            json.dump(values,stream);stream.flush();os.fsync(stream.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def main():
    path=Path('/opt/gab-review-data/integrations.private.json')
    ids=[]
    for flag in ('-u','-g'):
        result=subprocess.run(['docker','exec','gab-review','id',flag],capture_output=True,text=True)
        if result.returncode or not result.stdout.strip().isdigit():raise SystemExit('Сначала запустите контейнер gab-review')
        ids.append(int(result.stdout.strip()))
    values=json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(values,dict) or set(values)-{'DADATA_API_KEY','CHECKO_API_KEY'}:raise SystemExit('Неожиданный формат закрытого файла')
    for key,label in [('DADATA_API_KEY','DaData'),('CHECKO_API_KEY','Checko')]:
        value=getpass.getpass(label+' API key (Enter — сохранить прежнее значение): ').strip()
        if value:
            if len(value)>512 or any(c in value for c in '\r\n'):raise SystemExit('Некорректный формат ключа')
            values[key]=value
    write_private(path,values,*ids)
    print('Настройки сохранены с правами 0600. Обновите экран импорта; API-ключи не выводятся.')


if __name__=='__main__':main()
