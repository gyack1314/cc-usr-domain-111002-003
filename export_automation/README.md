# 供应商结算门户导出自动化模块（export_automation）

把一组**已确认的筛选条件**（账期 / 供应商组 / 币种）转换为门户导出文件，并让每个
文件都能解释“属于哪次导出”。全部针对本地合成门户，不连接真实财务系统。

## 要解决的问题

门户改成后台生成后，导出中心顶部可能已有其他用户的同名历史结果，文件名也不唯一
（恒为“结算资料.csv”）。直接下载最新一行会拿到上次筛选的文件。本模块用四条防线
保证关联正确：

1. **动作前监听**：生成点击前就挂好上下文级 `page` 事件与 `window.open` 取证脚本，
   新窗口（含与点击同帧产生的弹窗）不会丢；同名窗口被浏览器复用时也能证明
   “这次生成动作触发了开窗”。
2. **可见编号为证**：任务编号只采信页面 `#created` 与任务行 `data-job`，且要求编号
   在点击前不存在；拿不到可证明编号时只报告 `needs_manual`，绝不反复点击生成。
3. **三属性共同匹配**：导出中心按 *任务编号 + 创建者 + 筛选摘要* 同时匹配，不取最新
   一行；摘要与门户规范化协议 `账期|供应商组|币种` 逐字一致。
4. **文件元数据认领**：下载经浏览器 `download` 事件绑定到产生它的页面/任务，落盘后
   核对首行 `job_id`、`filter_digest`、`schema_version`；不符即移入隔离目录并写入
   原因，**异常文件永远不进正常输出目录**，不按文件名认领。

## 目录与状态机

```
<work-dir>/
├── run-state.json      # 每项条件的任务编号与轨迹（生成后立即落盘，可中断恢复）
├── manifest.json       # 交付清单：输入条件/门户任务/创建者/文件/校验结论/事件
├── output/             # 仅放元数据核对通过的文件，命名 <请求>__<任务>.csv
├── quarantine/         # 异常文件 + 同名 .reason.txt 隔离原因
├── staging/            # 下载暂存区，处理完即清空
└── browser-profile/    # 持久化浏览器档案（门户 localStorage 所在）
```

单项状态：`pending` → `completed` / `failed` / `timeout` / `window_closed` /
`needs_manual` / `quarantined`，后五者均为终态，恢复时不会重复触发门户动作。

## 命令行

```sh
# 隔离模式运行（输入文件按不可信数据对待）
python3 -I run_export.py batch  --input examples/conditions.json --work-dir ./run1
python3 -I run_export.py resume --input examples/conditions.json --work-dir ./run1
```

- `batch`：已有编号的条件直接找回，没有才生成一次；编号在等待前就写入状态文件，
  允许“保存任务编号后退出，下次优先找回”。
- `resume`：**只找回不新建**。无任何可证明编号的条件输出 `needs_manual`，
  门户任务数不会增加。
- 超时双预算：`--max-polls`（轮询次数）与 `--timeout`（总时限秒）任一耗尽即结束，
  输出 `timeout`；不使用 networkidle，也不靠固定秒数判断后台任务完成。
- 退出码：全部完成 `0`；存在任何非完成终态 `2`；输入用法错误 `64`。

## 有头演示（两个任务交错完成，各取各的文件）

```sh
python3 -I run_portal.py --port 8090 &              # 启动合成门户
Xvfb :99 -screen 0 1400x1000x24 &                   # 无显示器环境
DISPLAY=:99 python3 -I demo_interleaved.py --headed
```

产物在 `demo_assets/`：四张步骤截图（排队→B 先完成→A 后完成→终态）、
`interleaved-manifest.json` 与 `demo-output/` 下两份绑定各自任务的 CSV。

## 设计红线

- 业务代码只走页面 DOM 与浏览器事件；`/__control`、`/__state` 等运维入口不被
  任何业务流程调用（有 AST 测试守护）。
- 轮询只点该任务自己的“刷新任务”按钮，以 `.job-state` 可见中文文本判定
  排队/生成中/完成/失败。
- `PortalSession.inject_failure` 仅在受控测试里经 localStorage 制造“失败”终态，
  不访问任何 HTTP 运维接口。
