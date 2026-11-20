"""Bounded, resumable extraction for dense pages. Never repairs invented JSON."""
import hashlib
import json
import time
import urllib.request
from fastapi import HTTPException

TEXT_LIMIT=1200
OVERLAP=240
MAX_FRAGMENTS=32


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def split_text(text):
    """Cover every character; prefer full lines and repeat boundary context."""
    parts=[];start=0
    while start<len(text):
        end=min(start+TEXT_LIMIT,len(text))
        if end<len(text):
            newline=text.rfind('\n',start+TEXT_LIMIT//2,end)
            if newline>=0:end=newline+1
        parts.append(text[start:end])
        if len(parts)>MAX_FRAGMENTS:
            raise HTTPException(422,'Страница слишком большая: разделите документ на меньшие части')
        if end==len(text):break
        next_start=max(start+1,end-OVERLAP)
        newline=text.rfind('\n',max(start,next_start-OVERLAP),next_start)
        start=newline+1 if newline>=start else next_start
    return parts


def fragment_plan(request,page):
    messages=request.get('messages')
    if not isinstance(messages,list) or not messages or not isinstance(messages[-1],dict) or messages[-1].get('role')!='user':return None
    content=messages[-1].get('content')
    marker=f'Текст страницы с номером: {page}\n'
    if not isinstance(content,str) or marker not in content:return None
    prefix,_,text=content.partition(marker)
    if not text.strip():return None
    pieces=split_text(text)
    options=request.get('options') or {}
    if not isinstance(options,dict):raise HTTPException(422,'options должна быть объектом')
    planned=[]
    for index,piece in enumerate(pieces,1):
        prompt=(prefix+marker+f'Фрагмент {index} из {len(pieces)}. Извлеки только явно указанные здесь сведения. '
                'Границы перекрываются с соседними фрагментами. Не восстанавливай отсутствующие поля по догадке; '
                'оборванные записи пометь предупреждением.\n'+piece)
        planned.append({**request,'messages':[*messages[:-1],{**messages[-1],'content':prompt}],
                        'options':{**options,'num_ctx':4096,'num_predict':2000}})
    key=fingerprint({'mode':'fragments-v1','requests':planned})
    return key,planned


def extract_fragments(plan,reporter,page,total,*,clock=None):
    clock=clock or time.monotonic
    key,requests=plan
    result=reporter.page_result(page,key)
    if result is not None:return result
    deadline=clock()+1800
    results=[]
    for index,request in enumerate(requests,1):
        detail={'page':page,'completed':index-1,'total':len(requests)}
        reporter.report('llm',total,require_active=True,fragment=detail)
        request_hash=fingerprint(request)
        result=reporter.page_result(page,request_hash,scope='fragment')
        if result is None:
            remaining=deadline-clock()
            if remaining<=0:raise HTTPException(504,f'Страница {page}: время обработки исчерпано. Готовые фрагменты сохранены; повтор продолжит с них.')
            req=urllib.request.Request('http://ollama_ai:11434/api/chat',data=json.dumps(request,ensure_ascii=False).encode(),headers={'Content-Type':'application/json'},method='POST')
            try:
                with urllib.request.urlopen(req,timeout=min(300,remaining)) as response:
                    raw=response.read(1024*1024+1)
                if len(raw)>1024*1024:raise ValueError()
                result=json.loads(raw)
                parsed=json.loads(result['message']['content'])
                if result.get('done') is not True or result.get('done_reason')=='length' or not isinstance(parsed.get('records'),list) or any(not isinstance(r,dict) for r in parsed['records']):
                    raise ValueError()
            except Exception as exc:
                reason=result.get('done_reason') if isinstance(result,dict) else None
                count=result.get('eval_count') if isinstance(result,dict) else None
                reason=reason if reason in ('stop','length') else 'unknown'
                count=count if type(count) is int else 'unknown'
                raise HTTPException(502,f'Страница {page}, фрагмент {index}/{len(requests)}: Ollama не завершила корректный ответ ({type(exc).__name__}; done_reason={reason}; tokens={count}). Готовые страницы и фрагменты сохранены.') from None
            reporter.page_result(page,request_hash,result,scope='fragment')
        results.append(result)
        reporter.report('llm',total,require_active=True,fragment={**detail,'completed':index})
    # Overlap can produce repeated candidates. Keep them for manual review;
    # never silently merge two people/companies or infer missing contacts.
    records=[];seen={}
    for index,result in enumerate(results,1):
        for record in json.loads(result['message']['content'])['records']:
            warnings=[str(w) for w in record.get('warnings',[]) if isinstance(w,(str,int,float))] if isinstance(record.get('warnings'),list) else []
            signature=fingerprint({k:v for k,v in record.items() if k not in ('source_row','warnings')})
            if signature in seen and seen[signature]!=index:
                warnings.append('Возможный повтор на границе фрагментов — проверьте по оригиналу')
            seen[signature]=index
            notes=['Проверьте строку по оригиналу: страница обработана по частям',f'Источник: фрагмент {index} из {len(requests)} страницы {page}'] if len(requests)>1 else []
            records.append({**record,'warnings':list(dict.fromkeys([*warnings,*notes]))})
    combined={'model':requests[0].get('model'),'done':True,'done_reason':'stop',
              'message':{'role':'assistant','content':json.dumps({'records':records},ensure_ascii=False)},
              '_gab_fragment_count':len(requests)}
    for field in ('total_duration','load_duration','eval_duration','eval_count','prompt_eval_count'):
        combined[field]=sum(r.get(field,0) for r in results if type(r.get(field,0)) is int)
    reporter.page_result(page,key,combined)
    return combined
