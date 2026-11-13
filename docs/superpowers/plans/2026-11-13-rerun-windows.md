# 数据保护与Windows修复（用户已授权本机实施）

范围：保留人工status/reason/notes；全量重采来源去重；报价业务字段变化警告；Windows进程树清理与清理失败不吞结果。保留现有found/blocked口径。暂不做并发、页面改版、历史库清理或推送部署。

1. tests/test_rerun_integrity.py：临时SQLite验证重复来源、通过/跳过后成功与失败重采、报价单位/币种/分组/条件变化及顺序无变化；运行观察失败。
2. server.py及cloud_store.py：共用来源合并；在写事务内识别人工结论；错误分支不改人工结论；报价变化仅警告及日志。
3. tests/test_process_cleanup.py：隔离真实采集子进程夹具验证返回结果、停止、超时、清理故障保留结果；Windows原生CI验证Job关闭回收整树，包括父进程先退出。
4. server.py：平台启动参数、Windows Job与stdin启动闸门、收到结果最多等待正常退出2秒，随后有界强清理、停止立即清理、强制清理有界；错误仅日志不覆盖结果/原异常。
5. Python全量、Node既有回归；记录Windows与真实云端尚未验证边界；更新项目状态，不推送。
