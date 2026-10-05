import asyncio
import json
import uuid
from src import coworkers


class CoworkerListTool:
    async def execute(self, content, ctx):
        try:
            args=json.loads(content or '{}') if isinstance(content,str) else content or {}
            if not isinstance(args,dict) or set(args)-{'id'}:
                raise ValueError('Only optional coworker id is accepted')
            store=coworkers.Store()
            owner=(ctx or {}).get('owner')
            if args.get('id'):
                data={"coworker":store.get(owner,args['id']),"runs":store.history(owner,args['id'])}
            else:
                data={"coworkers":store.list(owner),"templates":coworkers.templates()}
            return {"output":json.dumps(data,ensure_ascii=False),"exit_code":0,**data}
        except (ValueError,LookupError,TypeError,KeyError) as exc:
            return {'error':str(exc),'exit_code':1}


class CoworkerSaveTool:
    async def execute(self,content,ctx):
        try:
            from src import agent_defs
            args=json.loads(content) if isinstance(content,str) else content
            if not isinstance(args,dict) or set(args)-{'coworker','id','expected_revision'}:
                raise ValueError('Provide coworker and optional id/expected_revision')
            row=coworkers.Coworker.model_validate(args['coworker'])
            if agent_defs.get(row.agent) is None:
                raise ValueError('Choose an existing agent definition')
            saved=coworkers.Store().save((ctx or {}).get('owner'),row,args.get('id'),args.get('expected_revision'))
            return {'output':json.dumps(saved,ensure_ascii=False),'exit_code':0,**saved}
        except (ValueError,LookupError,TypeError,KeyError) as exc:
            return {'error':str(exc),'exit_code':1}


class CoworkerRunTool:
    async def execute(self, content, ctx):
        from .subagent_tools import DelegateAgentsTool
        try:
            args=json.loads(content) if isinstance(content,str) else content
            if not isinstance(args,dict) or set(args)-{'id','mission','request_id'} or not isinstance(args.get('mission'),str) or not 1<=len(args['mission'])<=16000:
                raise ValueError('Provide coworker id, mission and optional request_id')
            owner=(ctx or {}).get('owner'); session=(ctx or {}).get('session_id')
            if not session:
                raise ValueError('Run a coworker from an existing chat session')
            request_id=args.get('request_id') or uuid.uuid4().hex
            if not isinstance(request_id,str) or not 1<=len(request_id)<=100:
                raise ValueError('Invalid request id')
            store=coworkers.Store(); row=store.get(owner,args['id'])
            previous=store.begin(owner,row['id'],request_id,session,args['mission'])
            if previous:
                return {"output":json.dumps(previous,ensure_ascii=False),"exit_code":0,**previous}
            instruction=f"Responsibility: {row['responsibility']}\nUser-maintained notes: {row['notes']}\nRelevant Hoards (check connections/tools first): {', '.join(row['hoards'])}\nCurrent mission: {args['mission']}"
            try:
                result=await DelegateAgentsTool().execute(json.dumps({"tasks":[{"name":row['name'],"agent":row['agent'],"instruction":instruction}],"parallel":False}),ctx)
            except asyncio.CancelledError:
                store.interrupted(owner,request_id)
                raise
            except Exception:
                store.interrupted(owner,request_id)
                raise
            store.finish(owner,request_id,result)
            return {**result,"coworker_id":row['id'],"request_id":request_id}
        except (ValueError,LookupError,TypeError,KeyError) as exc:
            return {"error":str(exc),"exit_code":1}
