"""Dense-page tests use synthetic text and mocked Ollama, never real investors."""
import importlib.util
import json
from pathlib import Path
import re
import unittest
from unittest.mock import MagicMock,patch
from fastapi import HTTPException

spec=importlib.util.spec_from_file_location('fragment_tests_module',Path(__file__).resolve().parents[1]/'services/gab-ocr/ollama_fragments.py')
fragments=importlib.util.module_from_spec(spec);spec.loader.exec_module(fragments)


def request():
    return {'model':'synthetic','stream':False,'format':{'type':'object'},
            'options':{'num_ctx':16384,'num_predict':6000},
            'messages':[{'role':'system','content':'Synthetic instructions'},
                        {'role':'user','content':'Extract records. Текст страницы с номером: 7\n'+''.join(f'Synthetic row {i:04} company contact data only\n' for i in range(100))}]}


class Reporter:
    def __init__(self):self.saved={};self.reports=[];self.cancel=False
    def page_result(self,page,key,result=None,scope='page'):
        if self.cancel:raise HTTPException(409,'cancelled')
        if result is not None:self.saved[(scope,page,key)]=result
        return self.saved.get((scope,page,key))
    def report(self,*args,**kwargs):
        if self.cancel:raise HTTPException(409,'cancelled')
        self.reports.append(kwargs.get('fragment'))


class FragmentTests(unittest.TestCase):
    def test_every_source_character_is_covered_with_bounded_overlapping_parts(self):
        for text in [''.join(f'{i:05}x\n' for i in range(1000)),''.join(f'{i:05}x' for i in range(1000))]:
            parts=fragments.split_text(text)
            cursor=0;previous=-1
            for part in parts:
                start=text.find(part,previous+1)
                self.assertGreaterEqual(start,0)
                self.assertLessEqual(start,cursor)
                self.assertLessEqual(len(part),fragments.TEXT_LIMIT)
                cursor=start+len(part);previous=start
            self.assertEqual(cursor,len(text))
        with self.assertRaises(HTTPException):fragments.split_text('x'*100000)

    def test_plan_preserves_prompt_schema_and_original_request(self):
        original=request();before=json.dumps(original,sort_keys=True)
        key,parts=fragments.fragment_plan(original,7)
        self.assertGreater(len(parts),1)
        self.assertEqual(key,'83002ffe88c03241880c5b3a3a89ed1e5a1f39fdf7af2eac7f54174d7a41ad93')
        self.assertEqual(fragments.fingerprint(parts[0]),'87b5a3147a41a38a144ea37edcb53047cb44e57203c9779e6119fa34dc8f4fdc')
        for part in parts:
            self.assertEqual(part['model'],original['model'])
            self.assertEqual(part['format'],original['format'])
            self.assertEqual(part['messages'][0],original['messages'][0])
            self.assertEqual(part['options'],{'num_ctx':4096,'num_predict':2000})
        self.assertEqual(json.dumps(original,sort_keys=True),before)
        self.assertIsNone(fragments.fragment_plan(original,8))

    def test_partial_failure_resumes_saved_fragments_without_repeating_them(self):
        plan=fragments.fragment_plan(request(),7);reporter=Reporter();calls=[];fail=True
        def model(req,**kwargs):
            payload=json.loads(req.data)
            index=int(re.search(r'Фрагмент (\d+) из',payload['messages'][-1]['content']).group(1))
            calls.append(index)
            self.assertLessEqual(kwargs['timeout'],300)
            if index==2 and fail:raise TimeoutError()
            response=MagicMock();response.__enter__.return_value=response
            response.read.return_value=json.dumps({'done':True,'done_reason':'stop','message':{'content':json.dumps({'records':[{'company_name':'Synthetic '+str(index)}]})}}).encode()
            return response
        with patch('urllib.request.urlopen',side_effect=model):
            with self.assertRaises(HTTPException) as error:fragments.extract_fragments(plan,reporter,7,8)
            self.assertIn('часть 2',error.exception.detail)
            self.assertEqual(len(reporter.saved),1)
            fail=False
            result=fragments.extract_fragments(plan,reporter,7,8)
            calls_before=len(calls)
            self.assertEqual(fragments.extract_fragments(plan,reporter,7,8),result)
            self.assertEqual(len(calls),calls_before)
        self.assertEqual(calls,[1,2,*range(2,len(plan[1])+1)])
        self.assertEqual(len(json.loads(result['message']['content'])['records']),len(plan[1]))
        self.assertTrue(all('Проверьте строку по оригиналу: страница обработана по частям' in r['warnings'] for r in json.loads(result['message']['content'])['records']))
        self.assertEqual(reporter.reports[-1],{'page':7,'completed':len(plan[1]),'total':len(plan[1])})

    def test_overlap_candidates_are_flagged_and_never_silently_dropped(self):
        plan=fragments.fragment_plan(request(),7);reporter=Reporter()
        for part in plan[1]:
            reporter.saved[('fragment',7,fragments.fingerprint(part))]={'done':True,'message':{'content':'{"records":[{"company_name":"Synthetic same"}]}'}}
        with patch('urllib.request.urlopen') as model:
            result=fragments.extract_fragments(plan,reporter,7,8)
            model.assert_not_called()
        rows=json.loads(result['message']['content'])['records']
        self.assertEqual(len(rows),len(plan[1]))
        self.assertIn('Возможный повтор на границе фрагментов — проверьте по оригиналу',rows[1]['warnings'])

    def test_deadline_and_cancellation_prevent_new_model_calls(self):
        plan=fragments.fragment_plan(request(),7)
        clock=MagicMock(side_effect=[0,1801])
        with patch('urllib.request.urlopen') as model:
            with self.assertRaises(HTTPException) as error:fragments.extract_fragments(plan,Reporter(),7,8,clock=clock)
            self.assertEqual(error.exception.status_code,504)
            model.assert_not_called()
            reporter=Reporter();reporter.cancel=True
            with self.assertRaises(HTTPException) as error:fragments.extract_fragments(plan,reporter,7,8)
            self.assertEqual(error.exception.status_code,409)
            model.assert_not_called()

    def test_truncated_json_is_split_with_fixed_limit_and_bounded_depth(self):
        plan=fragments.fragment_plan(request(),7);reporter=Reporter()
        response=MagicMock();response.__enter__.return_value=response
        response.read.return_value=json.dumps({'done':True,'done_reason':'length','message':{'content':'{"records":['}}).encode()
        with patch('urllib.request.urlopen',return_value=response) as model:
            with self.assertRaises(HTTPException):fragments.extract_fragments(plan,reporter,7,8)
            self.assertLessEqual(model.call_count,fragments.MAX_SPLIT_DEPTH+1)
            self.assertGreater(model.call_count,1)
            self.assertTrue(reporter.saved)
            self.assertTrue(all(key[0]=='split' for key in reporter.saved))
            for call in model.call_args_list:
                self.assertEqual(json.loads(call.args[0].data)['options']['num_predict'],2000)


    def test_truncated_parent_and_completed_child_are_not_regenerated_on_retry(self):
        plan=fragments.fragment_plan(request(),7);reporter=Reporter();calls=[];fail=True
        for index,part in enumerate(plan[1],1):
            if index!=2:reporter.saved[('fragment',7,fragments.fingerprint(part))]={'done':True,'message':{'content':json.dumps({'records':[{'company_name':f'Synthetic root {index}'}]})}}
        def model(req,**kwargs):
            payload=json.loads(req.data);content=payload['messages'][-1]['content']
            path=re.findall(r'Подфрагмент ([0-9.]+)\.',content)
            path=path[-1] if path else '2'
            calls.append(path)
            self.assertEqual(payload['options']['num_predict'],2000)
            if path=='2.2' and fail:raise TimeoutError()
            result={'done':True,'done_reason':'length','eval_count':2000,'message':{'content':'{"records":['}} if path=='2' else {'done':True,'done_reason':'stop','message':{'content':json.dumps({'records':[{'company_name':'Synthetic '+path}]})}}
            response=MagicMock();response.__enter__.return_value=response
            response.read.return_value=json.dumps(result).encode();return response
        with patch('urllib.request.urlopen',side_effect=model):
            with self.assertRaises(HTTPException):fragments.extract_fragments(plan,reporter,7,8)
            self.assertTrue(any(key[0]=='split' for key in reporter.saved))
            self.assertTrue(any(key[0]=='fragment' and '2.1' in str(value) for key,value in reporter.saved.items()))
            fail=False
            result=fragments.extract_fragments(plan,reporter,7,8)
            before=len(calls)
            self.assertEqual(fragments.extract_fragments(plan,reporter,7,8),result)
            self.assertEqual(len(calls),before)
        self.assertEqual(calls,['2','2.1','2.2','2.2'])
        self.assertEqual(len(json.loads(result['message']['content'])['records']),len(plan[1])+1)
        self.assertTrue(any(r and r.get('part')=='2.2' for r in reporter.reports))

    def test_adaptive_split_covers_text_and_respects_call_budget(self):
        original=request();plan=fragments.fragment_plan(original,7)
        part=plan[1][0];content=part['messages'][-1]['content']
        prefix,delimiter,text=content.partition('оборванные записи пометь предупреждением.\n')
        children=fragments.subdivide(part,prefix+delimiter,text,'1',0)
        left,right=[c[2] for c in children]
        self.assertTrue(text.startswith(left));self.assertTrue(text.endswith(right))
        self.assertGreater(len(left)+len(right),len(text))
        self.assertTrue(all(len(child[2])<len(text) for child in children))
        response=MagicMock();response.__enter__.return_value=response
        response.read.return_value=b'{"done":true,"done_reason":"length","message":{"content":"broken"}}'
        reporter=Reporter()
        with patch.object(fragments,'MAX_MODEL_CALLS',1),patch('urllib.request.urlopen',return_value=response) as model:
            with self.assertRaises(HTTPException) as error:fragments.extract_fragments(plan,reporter,7,8)
            self.assertEqual(error.exception.status_code,504)
            self.assertEqual(model.call_count,1)
        self.assertTrue(all(k[0]=='split' for k in reporter.saved))

    def test_invalid_saved_split_plan_stops_without_model_calls(self):
        plan=fragments.fragment_plan(request(),7);reporter=Reporter()
        reporter.saved[('split',7,fragments.fingerprint(plan[1][0]))]={'version':1,'children':['0'*64,'1'*64]}
        with patch('urllib.request.urlopen') as model:
            with self.assertRaises(HTTPException) as error:fragments.extract_fragments(plan,reporter,7,8)
            self.assertEqual(error.exception.status_code,409)
            model.assert_not_called()
