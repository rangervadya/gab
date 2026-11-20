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


MAX_SPLIT_DEPTH=3
MIN_SPLIT_TEXT=280
MAX_MODEL_CALLS=40


def subdivide(request,header,text,path,depth):
    if depth>=MAX_SPLIT_DEPTH or len(text)<=MIN_SPLIT_TEXT:return None
    middle=len(text)//2
    cuts=[i+1 for i in range(len(text)*2//5,len(text)*3//5) if text[i]=='\n']
    cut=min(cuts,key=lambda i:abs(i-middle)) if cuts else middle
    overlap=min(120,max(24,len(text)//8),cut-1,len(text)-cut-1)
    pieces=[text[:cut+overlap],text[cut-overlap:]]
    children=[]
    for index,piece in enumerate(pieces,1):
        child_path=f'{path}.{index}'
        child_header=header+f'Подфрагмент {child_path}. Извлеки только сведения из следующего текста.\n'
        child={**request,'messages':[*request['messages'][:-1],{**request['messages'][-1],'content':child_header+piece}]}
        children.append((child,child_header,piece,child_path))
    return children


def combine(request,results,labels):
    # Keep overlap candidates for explicit review, never guess entity merges.
    records=[];seen={}
    for index,result in enumerate(results):
        for record in json.loads(result['message']['content'])['records']:
            warnings=[str(w) for w in record.get('warnings',[]) if isinstance(w,(str,int,float))] if isinstance(record.get('warnings'),list) else []
            signature=fingerprint({k:v for k,v in record.items() if k not in ('source_row','warnings')})
            if signature in seen and seen[signature]!=index:
                warnings.append('Возможный повтор на границе фрагментов — проверьте по оригиналу')
            seen[signature]=index
            if len(results)>1:warnings.append('Проверьте строку по оригиналу: страница обработана по частям')
            if labels[index]:warnings.append(labels[index])
            records.append({**record,'warnings':list(dict.fromkeys(warnings))})
    result={'model':request.get('model'),'done':True,'done_reason':'stop',
            'message':{'role':'assistant','content':json.dumps({'records':records},ensure_ascii=False)},
            '_gab_leaf_count':sum(r.get('_gab_leaf_count',1) for r in results)}
    for field in ('total_duration','load_duration','eval_duration','eval_count','prompt_eval_count'):
        result[field]=sum(r.get(field,0) for r in results if type(r.get(field,0)) is int)
    return result


def extract_fragments(plan,reporter,page,total,*,clock=None):
    clock=clock or time.monotonic
    key,requests=plan
    result=reporter.page_result(page,key)
    if result is not None:return result
    deadline=clock()+1800
    model_calls=0

    def visit(request,header,text,path,depth,detail):
        nonlocal model_calls
        progress={**detail,**({'part':path} if depth else {})}
        reporter.report('llm',total,require_active=True,fragment=progress)
        request_hash=fingerprint(request)
        result=reporter.page_result(page,request_hash,scope='fragment')
        if result is not None:return result  # Old successful 0.4.8 fragments.
        resolved_hash=fingerprint({'mode':'adaptive-v1','request':request})
        result=reporter.page_result(page,resolved_hash,scope='fragment')
        if result is not None:return result
        marker=reporter.page_result(page,request_hash,scope='split')
        children=subdivide(request,header,text,path,depth) if header else None
        expected={'version':1,'children':[fingerprint(c[0]) for c in children]} if children else None
        if marker is not None:
            if marker!=expected:raise HTTPException(409,'Сохранённый план фрагмента не совпадает с текущим; требуется проверка')
        else:
            remaining=deadline-clock()
            if remaining<=0 or model_calls>=MAX_MODEL_CALLS:
                raise HTTPException(504,f'Страница {page}: бюджет обработки исчерпан. Готовые части сохранены; повтор продолжит с них.')
            model_calls+=1
            req=urllib.request.Request('http://ollama_ai:11434/api/chat',data=json.dumps(request,ensure_ascii=False).encode(),headers={'Content-Type':'application/json'},method='POST')
            result=None
            try:
                with urllib.request.urlopen(req,timeout=min(300,remaining)) as response:
                    raw=response.read(1024*1024+1)
                if len(raw)>1024*1024:raise ValueError()
                result=json.loads(raw)
            except Exception as exc:
                raise HTTPException(502,f'Страница {page}, часть {path}: ошибка Ollama ({type(exc).__name__}). Готовые страницы и части сохранены.') from None
            reason=result.get('done_reason') if isinstance(result,dict) else None
            count=result.get('eval_count') if isinstance(result,dict) else None
            if isinstance(result,dict) and result.get('done') is True and reason=='length' and children:
                # Only split metadata is persisted here: the truncated answer
                # is NOT a successful result and is never saved as records.
                reporter.page_result(page,request_hash,expected,scope='split')
            else:
                try:
                    parsed=json.loads(result['message']['content'])
                    if result.get('done') is not True or reason=='length' or not isinstance(parsed.get('records'),list) or any(not isinstance(r,dict) for r in parsed['records']):raise ValueError()
                except (KeyError,TypeError,ValueError,AttributeError) as exc:
                    safe_reason=reason if reason in ('stop','length') else 'unknown'
                    count=count if type(count) is int else 'unknown'
                    raise HTTPException(502,f'Страница {page}, часть {path}: некорректный JSON ({type(exc).__name__}; done_reason={safe_reason}; tokens={count}). Предел деления достигнут либо ответ повреждён. Готовые части сохранены.') from None
                reporter.page_result(page,request_hash,result,scope='fragment')
                return result
        parts=[visit(child,child_header,piece,child_path,depth+1,detail)
               for child,child_header,piece,child_path in children]
        resolved=combine(request,parts,[f'Источник: подфрагмент {c[3]} страницы {page}' for c in children])
        reporter.page_result(page,resolved_hash,resolved,scope='fragment')
        return resolved

    results=[]
    for index,request in enumerate(requests,1):
        content=request['messages'][-1]['content']
        delimiter='оборванные записи пометь предупреждением.\n'
        prefix,found,text=content.partition(delimiter)
        header=prefix+found if found else None
        detail={'page':page,'completed':index-1,'total':len(requests)}
        results.append(visit(request,header,text,str(index),0,detail))
        reporter.report('llm',total,require_active=True,fragment={**detail,'completed':index})
    labels=[f'Источник: фрагмент {i} из {len(requests)} страницы {page}' if len(requests)>1 else '' for i in range(1,len(requests)+1)]
    combined=combine(requests[0],results,labels)
    combined['_gab_fragment_count']=len(requests)
    reporter.page_result(page,key,combined)
    return combined
