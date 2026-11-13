# 数据保护及 Windows 进程修复

用户已确认先实施数据保护与 Windows 修复，本轮仅本机实现，不推送部署。基线 d26c9f5，保留最新前端改动。

1. 全量重采：共用 merge_sources 按网址和原文去重，原文变化保留。人工 reason 前缀对应的 status/reason/notes 在成功与失败路径保留；新报价仍写入并记录历史，不删除历史数据。
2. 报价变化：比较型号、折扣、输入/输出/缓存价格、币种/单位、分组/倍率、资源形态和条件；忽略顺序及证据刷新。人工结论不变，warnings 和事件提示复核；人工再次复核后清除提示。仅联系方式模式保持既有保存路径。
3. 进程：收到结果最多等待正常退出2秒，等待期间每50ms检查停止，之后有界强清理；清理失败只记日志，不覆盖返回结果或原始异常。reader负责关闭stdout，避免主线程关闭阻塞；父进程退出后半秒仍没有完整消息，报进程退出，不等待整站硬时限。
4. Windows：原方案 taskkill 无法回收已退出父进程的后代，改用 KILL_ON_JOB_CLOSE Job Object。worker在导入浏览器前等待stdin闸门；父进程先Assign再ready。Job初始化失败则停止worker且不发起浏览器/模型访问。Linux/macOS继续TERM/KILL进程组。参考：https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects 。Windows8+支持嵌套Job，实际兼容性待原生CI。
5. 验证：临时SQLite、真实隔离子/孙进程测试、既有Python/Node回归；添加独立Windows/Linux CI和可选临时Postgres专项。无生产空库测试、历史数据清理、并发或页面改版。
