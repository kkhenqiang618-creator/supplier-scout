"""Shared test helpers: capability gates, not assertions."""
import unittest

_READY = None

def runtime_ready():
    """True when the real browser runtime (Moli + Playwright) is installed on this machine.

    /api/run and Store.start_job('real') refuse to start without it, so tests that need a
    real batch skip instead of failing: the failure is the missing environment, not a bug.
    """
    global _READY
    if _READY is None:
        import browser_agent
        try: browser_agent.runtime_paths(); _READY = True
        except ValueError: _READY = False
    return _READY

requires_runtime = unittest.skipUnless(runtime_ready(), '需要 Moli 与 Playwright 浏览器运行时；本机未安装，跳过真实采集用例')
