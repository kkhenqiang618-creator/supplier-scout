"""Isolated autonomous agent worker. Parent can stop the full browser process group."""
import json
import sys
from browser_agent import run_agent,AgentServiceError,ModelTransientSiteError

def emit(value):print(json.dumps(value,ensure_ascii=False),flush=True)
if __name__=='__main__':
    try:
        result=run_agent(sys.argv[1],sys.argv[2],sys.argv[3],json.loads(sys.argv[4]),lambda message:emit({'kind':'log','message':message}))
        emit({'kind':'result','data':result})
    except AgentServiceError as error:emit({'kind':'error','message':str(error),'fatal':True,'partial':getattr(error,'partial',None)})
    except ModelTransientSiteError as error:emit({'kind':'error','message':str(error),'fatal':False,'model_transient':True,'partial':error.partial})
    except Exception as error:emit({'kind':'error','message':str(error),'fatal':False})
