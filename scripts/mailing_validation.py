"""Validate the existing sender without invoking it or changing workflows."""
import json
from urllib.parse import urlsplit


def credential_document(raw,expected_id):
    text=raw.decode()
    decoder=json.JSONDecoder()
    for index,char in enumerate(text):
        if char!='[':continue
        try:value,_=decoder.raw_decode(text[index:])
        except ValueError:continue
        if isinstance(value,list) and len(value)==1 and isinstance(value[0],dict) and value[0].get('id')==expected_id:
            return value[0]
    raise ValueError('Не удалось проверить credential почтового workflow')


def sender_references(workflow,url):
    path=urlsplit(url).path.removeprefix('/webhook/').strip('/')
    nodes={n['name']:n for n in workflow.get('nodes',[])}
    names=['Webhook','Prepare Mail','Send an Email','Build Success Response','Respond to Webhook']
    if not workflow.get('activeVersionId') or len(workflow.get('nodes',[]))!=5 or set(nodes)!=set(names):
        raise ValueError('Почтовый workflow не опубликован или его структура изменилась')
    expected_types=['webhook','code','emailSend','code','respondToWebhook']
    if any(nodes[name].get('type')!='n8n-nodes-base.'+kind for name,kind in zip(names,expected_types)):
        raise ValueError('Типы узлов почтового workflow изменились')
    hook=nodes['Webhook'];send=nodes['Send an Email']
    if hook['parameters'].get('path','').strip('/')!=path or hook['parameters'].get('authentication')!='headerAuth':
        raise ValueError('Почтовый webhook не соответствует CRM')
    if send['type']!='n8n-nodes-base.emailSend' or send.get('retryOnFail'):
        raise ValueError('SMTP-узел не соответствует проверенной конфигурации или автоматически повторяет отправку')
    if send['parameters'].get('toEmail')!='={{ $json.to_email }}':
        raise ValueError('SMTP-узел не использует подтверждённый адрес получателя')
    if not send['parameters'].get('fromEmail') or any(send['parameters'].get('options',{}).get(k) for k in ('ccEmail','bccEmail','cc','bcc')):
        raise ValueError('Проверьте отправителя и скрытые/дополнительные адреса')
    for a,b in zip(names,names[1:]):
        links=workflow.get('connections',{}).get(a,{}).get('main',[[]])[0]
        if len(links)!=1 or links[0].get('node')!=b:
            raise ValueError('Основная цепочка SMTP workflow изменилась')
    return hook['credentials']['httpHeaderAuth']['id'],send['credentials']['smtp']['id']
