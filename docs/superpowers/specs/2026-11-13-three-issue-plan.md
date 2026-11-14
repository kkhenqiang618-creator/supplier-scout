# 三类问题优化方案

# 一、Windows 上能真实采集、能停止

## 问题

每家网站在独立的子进程里采集。采完后，`collect_site` 用 `os.killpg` 清理子进程（[server.py:78](https://github.com/kkhenqiang618-creator/supplier-scout/blob/5c55e8e4a3c0dfc299f8895761d8858c1194e8df/server.py#L78)、[server.py:82](https://github.com/kkhenqiang618-creator/supplier-scout/blob/5c55e8e4a3c0dfc299f8895761d8858c1194e8df/server.py#L82)）。Windows 没有这个函数，导致两个后果：

- **结果丢失**：子进程输出结果时通常还没退出，所以每次都会走到清理这一步，并报 `AttributeError`。已拿到的结果被丢弃，这家网站被记成采集失败。实测 3 次都是这样。
- **停不下来**：点「停止」同样报这个错。实测 2 秒后子进程仍在运行。按代码逻辑，它会继续访问网站、调用模型，直到这一家自己跑完。

## 修复

- `server.py`：新增 `_terminate_tree(process)`，按平台结束整棵进程树：
  - **Windows**：启动子进程时加 `creationflags=subprocess.CREATE_NEW_PROCESS_GROUP`，对应现有的 `start_new_session`；清理时执行 `taskkill /T /F /PID <pid>`，Moli 和 Node 进程会一起结束。
  - **Linux / macOS**：保持现有的 `os.killpg`。
- `server.py`：`collect_site` 收到结果后，先等子进程自己退出，最多等 2 秒；超时才清理。清理出错只记日志，不丢弃已拿到的结果。
- `tests/test_process_cleanup.py`：新增。

## 判定口径

- **采集成功**：子进程返回了结果，就算这家采集成功。清理失败不影响结果。
- **停止**：点「停止」后 3 秒内，采集进程和它启动的 Moli、Node 进程全部结束。

## 测试

- `python -m unittest tests.test_process_cleanup -v`：用模拟子进程覆盖三种情况：正常结束、中途停止、子进程卡死。Windows 上验证 `taskkill`，Linux 上验证 `killpg`。
- 已在 Windows 本机验证：`taskkill /T /F` 能把孙进程一起结束。
- 新增 Windows 持续集成测试。仓库目前没有 `.github/workflows`，这类平台问题容易漏掉。

---

# 二、重复采集不污染已有数据

## 问题

「全量采集」有两处会把已有数据写坏。「仅采集联系方式」不受影响。

- **来源原文重复写入（仅本机）**：[server.py:366](https://github.com/kkhenqiang618-creator/supplier-scout/blob/5c55e8e4a3c0dfc299f8895761d8858c1194e8df/server.py#L366) 把「已有来源 + 本轮来源」整段交给 `save_result`，[server.py:232](https://github.com/kkhenqiang618-creator/supplier-scout/blob/5c55e8e4a3c0dfc299f8895761d8858c1194e8df/server.py#L232) 直接写入，没有去重。
  - 实测连跑两次全量采集，每家的来源条数从 1 变成 2，再变成 4。
  - 单条原文最多 3.5 万字，数据库会越积越大。
  - 云端写入前会去重，不受影响。
- **人工复核结论被覆盖（本机和云端）**：`save_result` 每次都按新报价重新计算状态。
  - 本机实测：先人工标为「符合」，全量重采后又变回「待复核」，只剩备注还在。
  - 云端去重之后调用的是同一个函数（[cloud_store.py:159](https://github.com/kkhenqiang618-creator/supplier-scout/blob/5c55e8e4a3c0dfc299f8895761d8858c1194e8df/cloud_store.py#L159)），读代码确认会同样被覆盖。
  - 这和《使用说明》第 44 行「人工通过或跳过的报价结论优先保留」不一致。

## 修复

- `server.py`：`Store.save_result` 写入前，按「网址 + 原文」合并来源。这段逻辑抽成本机和云端共用的 `merge_sources()`，写法和 [cloud_store.py:155](https://github.com/kkhenqiang618-creator/supplier-scout/blob/5c55e8e4a3c0dfc299f8895761d8858c1194e8df/cloud_store.py#L155) 相同。
- `server.py`：`Store.save_result` 遇到人工结论时，只更新报价、来源和检查时间，保留 `status`、`reason`、`notes`。云端也调用这个函数，改一处两边都生效。
- `server.py`：如果新报价和库里现有的报价不同，就在 `warnings` 里追加「报价已变化，建议重新复核」，并记一条日志。这只是提示：不改结论，不重新进入待复核列表，也不改表结构。
- `scripts/dedupe_sources.py`：新增一次性清理脚本，去掉已有数据里的重复来源。运行前先备份 `data/supplier.db`，支持只统计不修改的 `--dry-run`。
- `tests/test_rerun_integrity.py`：新增。

## 判定口径

- **同一条来源**：网址和原文都相同。网址相同但原文不同，算两条，都保留。这和云端现在的规则一致。
- **人工结论**：`reason` 以「人工复核：」开头。界面判断人工结论用的也是这个前缀（`static/workspace.js` 第 7 行）。
- **报价变化**：同一模型的 `ratio`、`input_price`、`output_price` 任一项不同。

## 测试

- `python -m unittest tests.test_rerun_integrity -v`：
  - 连跑两次全量采集，来源条数保持 1。
  - 人工标为「符合」后重采，仍是「符合」。
  - 报价变化时出现提示，结论不变。
- 云端在 `tests/test_cloud_store.py` 补同样的用例，运行时需要设置 `SCOUT_TEST_DATABASE_URL`。
- 清理脚本：在含重复来源的样例库上，先跑 `--dry-run`，确认只统计不修改；正式运行后，检查去重结果和备份文件。

---

# 三、名单变大后保持流畅、高效

## 问题

- **页面越用越卡**：
  - 前端每 1.5 秒请求一次 `/api/state`（[static/app.js:113](https://github.com/kkhenqiang618-creator/supplier-scout/blob/5c55e8e4a3c0dfc299f8895761d8858c1194e8df/static/app.js#L113)）。
  - 本机版每次返回全部供应商的全部字段，包括所有来源原文（[server.py:453](https://github.com/kkhenqiang618-creator/supplier-scout/blob/5c55e8e4a3c0dfc299f8895761d8858c1194e8df/server.py#L453)）。按 310 家、每家 3 段 1 万字估算，一次约 27 MB，每分钟约 1 GB。
  - 云端早已改成「列表只返回摘要，详情单独加载」，前端也支持，本机版没有用上。
- **串行采集太慢**：本机和云端都是一家采完再采下一家（[server.py:349](https://github.com/kkhenqiang618-creator/supplier-scout/blob/5c55e8e4a3c0dfc299f8895761d8858c1194e8df/server.py#L349)、[cloud_worker.py:120](https://github.com/kkhenqiang618-creator/supplier-scout/blob/5c55e8e4a3c0dfc299f8895761d8858c1194e8df/cloud_worker.py#L120)）。单家最长 14 分钟（全量）或 8 分钟（仅联系方式），310 家要跑很久。

## 修复

- `server.py`：`Store.state()` 增加摘要模式，`/api/state` 改用摘要：
  - SQL 不读取 `sources` 列，报价只保留前 3 条。
  - 字段和云端摘要一致（见 [cloud_store.py:190](https://github.com/kkhenqiang618-creator/supplier-scout/blob/5c55e8e4a3c0dfc299f8895761d8858c1194e8df/cloud_store.py#L190)），包括 `offer_count`、`offer_models`、`needs_price_review` 和 `_summary` 标记。
- `server.py`：新增 `GET /api/supplier?id=`，返回单家的完整数据，和云端的接口（[cloud_http.py:55](https://github.com/kkhenqiang618-creator/supplier-scout/blob/5c55e8e4a3c0dfc299f8895761d8858c1194e8df/cloud_http.py#L55)）一致。前端遇到 `_summary` 标记会自动去取详情（[static/app.js:18](https://github.com/kkhenqiang618-creator/supplier-scout/blob/5c55e8e4a3c0dfc299f8895761d8858c1194e8df/static/app.js#L18)），可以直接复用。
- `server.py`：导出 CSV（[server.py:458](https://github.com/kkhenqiang618-creator/supplier-scout/blob/5c55e8e4a3c0dfc299f8895761d8858c1194e8df/server.py#L458)、[server.py:474](https://github.com/kkhenqiang618-creator/supplier-scout/blob/5c55e8e4a3c0dfc299f8895761d8858c1194e8df/server.py#L474)）改为显式读取完整数据，导出内容不变。
- `static/app.js`：没有采集在跑时，改为每 10 秒刷新一次；页面切到后台时暂停刷新（监听 `visibilitychange`）。
- `server.py`：新增「同时处理 N 家」设置，默认 2，上限 4。
  - 用 `ThreadPoolExecutor` 同时运行 N 个采集子进程，写数据库仍然加锁。
  - 任务进度从一个数字改成每家各自的状态。
- `cloud_worker.py`：在任务的 `state` 里记录已完成的供应商 ID，替代单一的 `position` 进度，不改表结构。断点续跑时只跳过已完成的网站。
- `tests/test_state_summary.py`、`tests/test_parallel_batch.py`：新增，并补前端用例。

## 判定口径

- **列表与详情**：列表接口不返回来源原文，报价最多 3 条；完整内容只在打开详情和导出时读取。用上面的样例模拟，状态数据从 26.8 MB 降到约 0.1 MB。
- **并行采集**：
  - 同时运行的采集子进程不超过 N 个；
  - 一家失败或超时，不影响其他家；
  - 点「停止」后 3 秒内全部结束；
  - 中断后续跑，只跳过已经提交结果的网站。
- **并发上限**：云端浏览器容器内存为 2 GiB（`vercel.json`），默认并发数以实测不超内存为准。模型服务限流时可以调低。

## 测试

- `python -m unittest tests.test_state_summary -v`：310 家样例下，状态数据小于 1 MB；单家详情和导出 CSV 的内容与改造前一致。
- `python -m unittest tests.test_parallel_batch -v`：
  - N=3 时，同时运行的子进程不超过 3 个；
  - 「停止」能结束全部子进程；
  - 续跑不会重复采集；
  - 用固定的模拟结果对比，并行和串行写入的数据一致。
- `node --test tests/test_workspace_frontend.cjs`：没有采集时刷新间隔为 10 秒、切到后台会暂停刷新、打开详情时单独加载。
