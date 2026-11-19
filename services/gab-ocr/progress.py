"""Progress callbacks to a fixed CRM destination; never to arbitrary URLs."""
import json
import os
import urllib.error
import urllib.request
from fastapi import HTTPException


class Reporter:
    def __init__(self,batch_id=None,token=None):
        self.batch_id=batch_id if isinstance(batch_id,int) and batch_id>0 else None
        self.token=token if isinstance(token,str) and len(token)==32 else None

    def report(self,stage,total,page_completed=None,require_active=False):
        if not self.batch_id or not self.token:return
        request=urllib.request.Request(f'http://gab-review:8000/api/mailings/imports/{self.batch_id}/progress',
            data=json.dumps({'stage':stage,'total':total,'page_completed':page_completed}).encode(),
            headers={'Content-Type':'application/json','X-GAB-Worker-Token':self.token},method='POST')
        try:
            with urllib.request.urlopen(request,timeout=3) as response:response.read(1000)
        except urllib.error.HTTPError as exc:
            if exc.code in (403,409):raise HTTPException(409,'Обработка документа остановлена') from None
            if require_active:raise HTTPException(503,'Не удалось проверить состояние обработки') from None
            # Progress outages must not invent success or discard valid OCR text.
        except (OSError,TimeoutError):
            if require_active:raise HTTPException(503,'CRM временно недоступна') from None
