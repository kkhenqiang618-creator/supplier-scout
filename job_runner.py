"""The supplier batch loop shared by the local workspace and the cloud worker.

Ordering, cancellation checks and the consecutive model-failure limit live here;
persistence, progress reporting and job bookkeeping stay behind the target object,
so the SQLite workspace and the Postgres-backed worker keep their own rules.
"""
from collection import CollectionCancelled,BatchRuntimeError,AgentServiceError,ModelTransientSiteError

MODEL_FAILURE_LIMIT=3
MODEL_FAILURE_MESSAGE='模型服务连续 '+str(MODEL_FAILURE_LIMIT)+' 家不可用，批次已暂停；可稍后继续'

class BatchTarget:
    """Hooks run_batch needs. Implementations own storage, progress and job state."""
    def active(self):
        """Return False once the batch must stop before touching the next site."""
        raise NotImplementedError('active')
    def start_site(self,row):
        """Report that this site is about to be opened."""
        raise NotImplementedError('start_site')
    def collect(self,row,mode,scope):
        """Run the collection for one site and return its result."""
        raise NotImplementedError('collect')
    def save_success(self,row,result,scope,index):
        raise NotImplementedError('save_success')
    def save_model_transient(self,row,error,scope,index):
        raise NotImplementedError('save_model_transient')
    def save_agent_failure(self,row,error,scope,index):
        raise NotImplementedError('save_agent_failure')
    def save_site_failure(self,row,error,scope,index):
        raise NotImplementedError('save_site_failure')
    def batch_failed(self,error):
        """The browser runtime is unusable: stop without marking sites as blocked."""
        raise NotImplementedError('batch_failed')
    def model_failures_exhausted(self):
        raise NotImplementedError('model_failures_exhausted')

def run_batch(rows,mode,scope,target,start=0,fatal=()):
    """Visit each row once.

    `start` is the position of the first row in the full job (the cloud worker
    resumes mid-job). Errors listed in `fatal` (for example a lost job lease)
    propagate instead of being recorded as a site failure. Returns True when the
    loop stopped early because the batch itself failed (the target has already
    recorded that), False when every row was visited.
    """
    model_failures=0
    for offset,row in enumerate(rows):
        index=start+offset
        if not target.active():raise CollectionCancelled()
        target.start_site(row)
        try:
            result=target.collect(row,mode,scope)
            target.save_success(row,result,scope,index)
            model_failures=0
        except CollectionCancelled:
            raise
        except BatchRuntimeError as error:
            if not target.active():raise CollectionCancelled()
            target.batch_failed(error);return True
        except ModelTransientSiteError as error:
            if not target.active():raise CollectionCancelled()
            target.save_model_transient(row,error,scope,index)
            model_failures+=1
            if model_failures>=MODEL_FAILURE_LIMIT:
                target.model_failures_exhausted();return True
        except AgentServiceError as error:
            if not target.active():raise CollectionCancelled()
            target.save_agent_failure(row,error,scope,index);return True
        except Exception as error:
            if isinstance(error,fatal):raise
            if not target.active():raise CollectionCancelled()
            target.save_site_failure(row,error,scope,index)
    return False
