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
        self.assertEqual(len(key),64)
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
            self.assertIn('фрагмент 2/',error.exception.detail)
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

    def test_truncated_json_is_not_stored_and_not_retried_with_a_larger_limit(self):
        plan=fragments.fragment_plan(request(),7);reporter=Reporter()
        response=MagicMock();response.__enter__.return_value=response
        response.read.return_value=json.dumps({'done':True,'done_reason':'length','message':{'content':'{"records":['}}).encode()
        with patch('urllib.request.urlopen',return_value=response) as model:
            with self.assertRaises(HTTPException):fragments.extract_fragments(plan,reporter,7,8)
            self.assertEqual(model.call_count,1)
            self.assertEqual(reporter.saved,{})
