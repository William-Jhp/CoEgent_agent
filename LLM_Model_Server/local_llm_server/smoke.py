"""Small structured-output API probe; no model or CoEgent imports."""
import json
import time
from urllib.request import Request, urlopen


def schema_probe(endpoint,model,timeout):
    schema = {'type':'object','properties':{'probe':{'const':'schema_applied'}},
              'required':['probe'],'additionalProperties':False}
    payload = {'model':model,'temperature':0,'max_tokens':64,'stream':False,
               'messages':[{'role':'user','content':
                   'Return exactly {"unexpected":true}. Do not include a probe field.'}],
               'response_format':{'type':'json_schema','json_schema':{
                   'name':'server_contract_probe','strict':True,'schema':schema}}}
    started = time.monotonic()
    data = chat_completion(endpoint,payload,timeout)
    try:
        parsed = json.loads(data['choices'][0]['message']['content'])
    except (TypeError,json.JSONDecodeError) as exc:
        raise ValueError('invalid structured-output response') from exc
    if parsed != {'probe':'schema_applied'}:
        raise ValueError('server did not satisfy the requested JSON schema')
    return {'passed':True,'content':parsed,'elapsed_s':round(time.monotonic()-started,3),
            'usage':data.get('usage')}


def chat_completion(endpoint,payload,timeout):
    """Require a completed chat result, including on the actual plan request."""
    req = Request(endpoint.rstrip('/')+'/v1/chat/completions',json.dumps(payload).encode(),
                  {'Content-Type':'application/json'})
    with urlopen(req,timeout=timeout) as response:
        raw = response.read(1024*1024+1)
        if len(raw)>1024*1024:
            raise ValueError('chat response exceeds limit')
        data = json.loads(raw)
    try:
        choice = data['choices'][0]
        if choice.get('finish_reason') != 'stop':
            raise ValueError('chat response truncated or incomplete')
        if not isinstance(choice['message']['content'],str):
            raise ValueError('chat content must be a string')
    except (KeyError,IndexError,TypeError) as exc:
        raise ValueError('invalid chat completion response') from exc
    return data
