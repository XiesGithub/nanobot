# 第3阶段：五题程序评测与核心轨迹

只实施 V001–V004、V009，保留原题的结构、类型、工具和副作用要求。入口是继承的
`AgentLoop.process_direct`；隔离适配器仅覆写默认工具装配，不改变 Loop/Runner 的任务逻辑。
阶段1检查由 `common.phase1_audit` 执行；Case loader 校验来源、fixture 哈希和受支持谓词。

在仓库根目录使用 Conda `nanobot`：

```powershell
conda run --no-capture-output -n nanobot python -m evals.run --mode D --repeat 2 --selfcheck --output tmp/eval-D-new
conda run --no-capture-output -n nanobot python -m evals.run --mode R --output tmp/eval-R-new
conda run --no-capture-output -n nanobot python -m evals.selfcheck --output tmp/eval-trace-selfcheck-new
conda run -n nanobot python -m pytest tests/evals -q
conda run -n nanobot ruff check evals tests/evals
```

输出目录必须不存在；每次运行冻结独立 manifest、gold、trial、JSONL 事件、前后快照哈希、
独立进程评分的 verdict、JSON 汇总及 Markdown 报告。旧结果不覆盖。正常退出码0表示
所选程序诊断全部满足；失败/无效/未执行/负例漏检返回1。R仅运行每题一次；D可重复两次。
在限制 Windows 命名管道的执行沙箱中，需要允许该命令创建子进程通信管道。

每个 trial 使用 spawn 新进程、空会话、独立 config/runtime/workspace，不读取用户会话，
不加载真实邮箱/MCP/插件工具。仅装配真实 read_file/write_file/edit_file/list_dir，
路径边界进一步收紧到业务工作区，连 runtime/media 也不向工具开放。此隔离是工具权限边界，
不是 OS 沙箱；不适用于任意代码执行题。父进程经 IPC 保存证据，Agent 不可读写评分标准和产物。

运行目录仅允许创建/更新本 trial 的一个精确 session 文件，配置保持只读。所有业务文件新增、
删除和修改均比较；工具 execute 的独立审计能发现“写错后恢复”。固定 cli/eval 的
`_progress=true`、无附件通知单列为内部进度，不计业务外发；全量 outbox 元数据仍保留。
其他消息、禁止工具请求和越界访问均不能因工具未执行而隐去。

任务窗口 D=15秒、R=120秒；初始化30秒，工具请求16次、迭代8轮、R模型attempt最多8次，
单次输出2048。D超时控制专用窗口为0.3秒，脚本等待60秒，由父进程终止并回收进程树。
run窗口、已观察Token、调用数也有限制；在同一冻结计划中，未启动项保留在报告里。
凭据仅在R可信Provider进程从根目录api.txt读取，不复制到实例、不放入环境变量；
禁用第三方遥测和HTTP日志。SDK重试0，保留Provider standard重试；请求模型严格固定
deepseek-v4-flash，thinking disabled，temperature 0。记录服务返回的模型ID/指纹，别名不同不隐藏。

自检包含4个故意错误：V001错误JSON值、V009虚假完成、V009误改无关文件、V001超时。
它们属于诊断控制，不是新增题，也不进入模型能力分母。D脚本响应会走真实上下文、会话和文件工具；
R没有脚本指示工具选择。评分器另有类型、重复键、顺序、证据缺失、篡改、越界和清理失败测试。

当前任务 profile 为 `D-phase3-diagnostic-v1` / `R-phase3-diagnostic-v1`，轨迹协议为
`phase3/1.0`。`evals.selfcheck` 使用 `runner_contract` 替身传输，验证11种观测控制，
真实模型调用数为0。这些 profile 仍属于程序诊断：

- 本阶段 profile 只输出程序断言结果，`formal_quality_eligible=false`；独立的第4阶段归档语义评分入口见下文，不改变这些历史执行结果。
- 尚无经过验证的输入Token上界，不能宣称实现了调用前硬Token预留；已知用量超限判失败，
  缺用量且没有已证实失败时判未判定。金额未知为null，不是0。
- 已覆盖任务入口、Loop阶段、Hook迭代、逐次Provider调用、重试等待、工具请求与实际执行、
  异常和取消。后台、渠道、子Agent及其他复杂功能的覆盖留待后续阶段。
- 全部统计只覆盖这5题，其他115题不运行、不改成“已实施”。

事件以 `run_id` / `trial_id` / `task_id` 关联，`span_id` / `parent_span_id` 保留调用树；
每个 producer 单独保留序号和时钟身份。父进程保存 `events.jsonl`、独立状态快照、
`artifacts/trace-analysis.json`、证据哈希及 verdict。不同进程的 monotonic 时间不直接相减。
硬退出由 watchdog 引用未结束 span，不伪造模型或工具终态。

账本只按真实 `model_attempt` 汇总；Hook/Runner汇总和SDK传输诊断不重复收费。
D响应单列 `scripted_response`。缺失/部分 usage、SDK覆盖未知及不完整轨迹保留未知值，
不能由 parser 的零占位推出已知零用量。采集失败不修改Agent结果，但使证据无效。

版本化实测及恢复说明见 [第3阶段报告](../docs/agent-evaluation-phase3-report.md)；
历史 [第2阶段报告](../docs/agent-evaluation-phase2-report.md) 和旧产物保持原样。

## 第4阶段：固定归档语义评分

阶段4使用 `evals.semantic_run` 评分五个 V005/V007 固定答案包，并对六类、正负各一的12项裁判控样进行双上下文自检。它只审查归档文本，不启动 Agent、不执行完整任务，也不扩展 120 题集。V007 的归档不含工具轨迹，不据此判定文件是否实际读取。

在仓库根目录使用 Conda `nanobot`。每次 `--output` 目录必须不存在；为保留原始证据，不要复用已有目录：

```powershell
conda run --no-capture-output -n nanobot python -m evals.semantic_run --mode D --output tmp/eval-phase4-D-new
conda run --no-capture-output -n nanobot python -m evals.semantic_run --mode R --output tmp/eval-phase4-R-new
```

D 使用冻结脚本响应验证 gold/裁判协议，不构成模型评分能力证据。R 使用独立裁判请求和新上下文，模型固定为 `deepseek-v4-flash`，密钥由可信运行器从根目录 `api.txt` 读取。裁判不连接 AgentLoop，也无工具权限。Gold 在候选答案评分前审查并冻结；两个初审遇到分歧、未知或异议时最多复判一次，未解决项不会通过。程序硬失败由独立聚合保留，不能被裁判覆盖。

输出包含版本化 manifest、包与引用哈希、自检、gold 审查、程序及语义 verdict、逐次裁判尝试 `calls.jsonl`、费用汇总和 artifact index。产品调用/成本与裁判费用分开；未知 usage 或价格按 `null` 处理。输入 token 上界当前是 UTF-8 字节启发式，不能视为服务端硬保证。

最终固定档案验证使用 `phase4/1.1`、prompt `semantic-review-prompt/1.1`，rubric/gold 为1.0。D-v1.1 与 R-v1.1 均完成：两份 gold 通过、五个候选标签全部匹配；档案结果为2 pass、3个预期 `product_fail`。R-v1.1 的12项双上下文自检一致12/12（24项判断，false pass/reject 0/12），16次实际评分调用共45,170 Token，金额 `null`。请求模型为 `deepseek-v4-flash`，服务返回ID为 `deepseek-flash`；记录为服务别名/漂移，不推断客户端 fallback。首次 R-v1.0 严格拒绝错误 evidence 键，失败记录保留。两版真实模型评分共18次调用、56,788 Token，价格未知、总金额 `null`；产品调用0，未执行 Agent。完整结果见[第4阶段报告](../docs/agent-evaluation-phase4-report.md)、[R-v1.1汇总](../docs/agent-evaluation-phase4-runs/R-v11-20261010/run-result.json)和[交付审计](../docs/agent-evaluation-phase4-runs/delivery-audit.json)。测试证据为[v1.0契约测试](../docs/agent-evaluation-phase4-runs/contract-tests.xml)及[v1.1定向测试](../docs/agent-evaluation-phase4-runs/v11-tests.xml)。

本阶段只覆盖归档语义评分：未执行 Agent、未运行 `full_task`，也未扩展120题。后续完整任务适配留待路线后续阶段。

## 第5阶段：三次独立重复与指标

`evals.repeat` 冻结 V001–V004、V009 每题三次的15项主计划，复用新进程、空会话、独立工作区、程序评分和阶段3事件。每次启动都重新初始化 fixture；全部 trial、原始 JSONL、评分证据与结果保留，输出目录必须是新目录。

```powershell
conda run --no-capture-output -n nanobot python -m evals.repeat --mode D --controls --output tmp/eval-phase5-D-new
conda run --no-capture-output -n nanobot python -m evals.repeat --mode R --output tmp/eval-phase5-R-new
```

`--controls` 仅允许D模式：四个错误控制、一次 Provider 503、一次工具错误及一次目标工具未被调用的故障计划。故障在真实 Provider/工具执行边界记录，503走已有重试，工具错误后的再次调用由确定性脚本提供；不代表真实模型恢复能力。控制不进入15项主计划的成功率、延迟和成本分母。

`manifest.json` 固定计划、模式、范围和版本；`records.jsonl` 每次执行后落盘，`metrics.json` 根据冻结计划关联全部记录，`run-result.json` 保留明细、三次重置证据及诊断结果。执行、评分、观测完整率分别计算；漏题、重复ID或未判定项阻止覆盖范围严格率的发布。每题展示成功次数，另报三次稳定成功率、不稳定率和逐题平均成功率。

汇总前核验原始 artifact 哈希、事件与证据一致性，并对已验证证据重算确定性程序判定，与独立评分进程的 verdict 精确比对；完整但虚假的全通过文件也会被拒绝。trial 必须与冻结计划匹配，费用按当前 profile 和逐次 attempt 账本核验，不能由被改写的 trial 费用字段变成零。

P95使用 nearest-rank，只统计有完成证据的样本，并列失败耗时、超时率及任务类型/入口/故障条件分组。完成时间以同一 worker 时钟的 `entry.start` 至 `delivery` 计量，排除清理；失败总耗时保留父进程看门狗及回收时间。安全分别报告尝试、拦截、实际违例，关键实际违例阻断通过；未知侧效不能称安全通过。

产品费用包括失败和重试，程序评分费用单列；缺失用量、单价、币种或成本窗口未闭合时金额保持 `null`，零成功的成功成本不可定义。故障恢复只统计已实际触发、预设可恢复且结果已判定的项，未触发、永久失败和取消另列。

发布范围仍是五题程序诊断，`formal_quality_eligible=false`。D与R、主计划与控制分别统计，V005/V007归档答案不进入任务能力分母；未覆盖的115题、渠道、后台、子Agent及付费工具不能据此发布成绩。少量样本只作诊断。

实施与验证证据见[第5阶段报告](../docs/agent-evaluation-phase5-report.md)。

## 第6阶段：120项登记与分批模块验证

`evals.batches` 登记全部120项原题，按累计24/60/120项运行。每项冻结入口、fixture、声明范围、程序断言及限制，新进程/独立目录执行；危险场景和环境缺口写入未判定，不删除分母。当前98项实际完成（93通过、5产品失败），22项未判定，全部为D模式诊断；不发布120题真实模型或整题成绩。

```powershell
conda run --no-capture-output -n nanobot python -m evals.batches --limit 24 --output tmp/phase6-24-new
conda run --no-capture-output -n nanobot python -m evals.batches --limit 60 --reuse-from tmp/phase6-24-new --output tmp/phase6-60-new
conda run --no-capture-output -n nanobot python -m evals.batches --limit 120 --reuse-from tmp/phase6-60-new --output tmp/phase6-120-new
conda run --no-capture-output -n nanobot python -m evals.batch_audit tmp/phase6-120-new --report tmp/phase6-audit-new.json
```

输出和审计报告路径必须是新路径。`--reuse-from` 先离线审计旧证据，并要求契约完全相同；复制并保留 trial、原始 run ID 与递归来源源码快照。`--cases V118 V120` 仅用于定向验证；`--rerun-invalid` 只能显式重跑旧结果为 invalid 的项，不能抹掉旧结果或无故重跑有效通过项。

实现或 fixture 来源发生改变时，可用 `--refresh-cases` 明确指定需要重新验证的项，并提供 `--refresh-reason`；必须存在保留的旧证据，原因与来源记录到新 manifest。未指定项继续复用，旧失败不覆盖。源码快照同时保存所复用的邮件测试 helper。

事件、文件证据、逐项 verdict、清理和里程碑独立保存；评分缺失保持未知，费用只计实际模型 attempts，程序评分单列。本机未验证 OS 沙箱，V022/V104 不执行；MockTransport 私网探测不产生真实出站。实际浏览器、授权渠道和部分整题语义场景保留缺口。后台/投递观测只在可信运行器绑定的上下文内有效，不代表生产 gateway 的全局长期采集。

完整结果、已知产品失败、平台限制和验证记录见[第6阶段报告](../docs/agent-evaluation-phase6-report.md)及[120项状态清单](../docs/agent-evaluation-case-status.md)。

## 第8阶段：日常诊断与独立固定负载

所有命令在 Conda `nanobot` 环境执行，输出目录必须新建；不会复用历史成绩。

```powershell
conda run --no-capture-output -n nanobot python -m evals.daily --tier development --mode D --output tmp/dev-new
conda run --no-capture-output -n nanobot python -m evals.daily --tier smoke --mode D --output tmp/smoke-new
conda run --no-capture-output -n nanobot python -m evals.daily --tier nightly --mode D --output tmp/nightly-new
conda run --no-capture-output -n nanobot python -m evals.daily --tier release --mode D --output tmp/release-new
conda run --no-capture-output -n nanobot python -m evals.daily --audit tmp/dev-new --report tmp/dev-audit.json
conda run --no-capture-output -n nanobot python -m evals.performance --platform windows --output tmp/load-new
```

开发默认5题，可用 `--cases V001 ...` 限定至已有公开适配（至多10题）；冒烟24×1，夜间60×3，发布60公开+20独立保留×5。
发布每轮用新UUID生成事实，独立重算gold、检查公开120题及fixture去重，冻结并一次性登记；普通CI不读取保留gold。
这是独立事实保留集，不代表模型从未见过题型。答案与完整源快照保存在可信报告目录，Agent只得到隔离输入和fixture。

真实模型必须显式 opt-in 并给出三项正整数预算。例如单题验证：

```powershell
conda run --no-capture-output -n nanobot python -m evals.daily --tier development --mode R --cases V001 --max-attempts 8 --max-tokens 32000 --max-wall-seconds 180 --opt-in-real-model --output tmp/real-new
```

只使用 `deepseek-v4-flash` 和根 `api.txt`；凭据不进快照或Agent目录。每次SDK请求执行前计数并保守预留输入UTF-8字节+最大输出；已知usage逐次扣除，未知usage/异常停止后续请求。
这是本地请求/token/时间上限，不是供应商硬性账单封顶；价格未知时费用保持null且不得质量通过。评分为程序评分，费用单列为0；未接入实际Agent的R模块仍标未判定。

`run-result.json` 的状态为 `passed_diagnostic / regression / invalid / insufficient_evidence`；除第一项外退出码为1，当前所有profile的 `quality_passed=false`。
程序失败无法被评分改为通过；执行/评分/观测完整率按原计划分母统计，Module与Agent范围分开，模块诊断没有全任务安全证明。
保存逐次JSONL、输入契约、原始轨迹与文件证据、评分版本、源/环境身份、预算、清理和SHA索引，可离线重算，审计不重跑Agent或模型。

`.github/workflows/evaluation.yml` 将工程测试、D和R分开归档30天；push/PR运行开发+冒烟，定时运行夜间，手动D可运行发布。
2026-10-15起，工程、D、固定负载和有界R都有Windows/Linux profile，工程命令统一使用Conda nanobot。
CI的R仅允许冻结的 `evals/ci-real-plan.json`：development/V001每平台1次，单平台4 attempts、16,000 tokens、120秒，总上限8/32,000/240；旧本地CLI示例不是CI授权预算。
R先要求工程、D开发、固定负载及下载后离线重放成功，并通过可信分支、显式opt-in和保护配置API检查。
保护环境必须有reviewers、防自审及精确main/nightly分支规则；查询失败即拒绝，不以环境名称证明保护已配置。
只有受保护R步骤可读取 `NANOBOT_EVAL_API_KEY`；普通D完全不读取api.txt。配置存在、shell解析或本地执行均不等于远程运行验收。
Linux/Windows固定负载为独立矩阵：5份同V001输入、并发2、每份空缓存workspace/runtime；OS页缓存不保证冷，机器/数据身份会归档。
平台不匹配退出失败；与baseline比较必须有完整、同机器/同条件且已审计的基线，否则证据不足。D与固定负载不会替代真实模型成绩。

CI监督与归档重放入口（输出必须为新目录；审计报告放在原目录外）：

```powershell
conda run --no-capture-output -n nanobot python -m evals.ci --kind daily --platform windows --tier development --mode D --timeout 3900 --output tmp/ci-D-new
conda run --no-capture-output -n nanobot python -m evals.ci --kind performance --platform windows --timeout 1800 --output tmp/ci-load-new
conda run --no-capture-output -n nanobot python -m evals.ci --audit tmp/ci-D-new --report tmp/ci-D-audit.json
```

监督器保存runner环境、源码文件身份、远程run URL（本地为null）、命令退出、进程回收、原始证据和离线重放。
执行成功且原始状态passed_diagnostic、重放有效才退出0；失败/invalid/证据不足退出1、超时124、中断130。质量资格始终false。
`--audit`退出0只证明封存收据可重放：准确记录的环境阻断也可能审计有效，但其raw_replay_ok=false，原执行仍失败。
上传使用always并拒绝无文件，下载后再离线重放；runner断电/硬杀不能保证最终报告或上传，须结合远程conclusion保留未闭合。
普通Linux runner不是专用OS隔离环境；本profile不授权危险exec、越界符号链接或SSRF绕行场景。

## 第7阶段：冻结版本与配对门禁

`evals.identity` 将当前未提交代码、提示模板、技能、工具、验证集、程序评分和裁判、项目配置、依赖及运行环境冻结为内容哈希快照。快照排除凭据、缓存与历史运行产物；候选必须保持验证集、裁判、环境、运行条件和评测运行器一致。Agent 代码及提示可作为候选差异，项目配置变化需要另建可比条件。源码只在 `tmp/phase7-snapshots/` 的新目录中运行，完成后回收。

```powershell
conda run --no-capture-output -n nanobot python -m evals.paired freeze --source . --mode D --output tmp/phase7-baseline-new
conda run --no-capture-output -n nanobot python -m evals.paired freeze --source path/to/candidate --mode D --output tmp/phase7-candidate-new
conda run --no-capture-output -n nanobot python -m evals.paired run --baseline tmp/phase7-baseline-new --candidate tmp/phase7-candidate-new --mode D --module-inventory --output tmp/phase7-paired-new
conda run --no-capture-output -n nanobot python -m evals.paired audit --run tmp/phase7-paired-new --report tmp/phase7-audit-new.json
```

输出路径必须是新路径。配对集为已有5题和新建20题保留集，各版本每题独立三次、交错执行，逐次保留轨迹、文件、判断和费用证据。保留集独立生成虚构事实和 gold，核对公开120题输入及 fixture 的字面/哈希重合；共享能力类型，不声称模型从未见过这类题。保留集从首次预留开始计入暴露账本，中断也不重置；新比较轮次必须替换保留集，不能针对本轮答案调优后继续声称独立评估。可在已存在的结果上重复离线审计，不重跑 Agent。

`--module-inventory` 从基线快照全新执行120项模块登记，保留未判定项；它不并入25题的配对分母。历史阶段成绩不能进入当前比较。结果按公开、保留和合计分别报告，完整性或条件不符则拒绝；逐题质量、安全或可靠性退步不会被平均改善覆盖。以题为聚类单位保留三次重复，计算95% bootstrap 区间和符号检验；少于10题、区间跨零或同快照不能宣布改善。默认门禁要求成功率、三次可靠性和安全，性能/费用/恢复指标可通过冻结策略列为必需；未知费用、基线为零或未触发故障使相应比较不可判定。

D 是脚本响应的运行器诊断，不能证明模型或泛化能力；当前交付用同版本对照验证流程，没有优化候选。R 使用 `deepseek-v4-flash`、每次最多8次请求/32,000已知Token、全轮最多64次请求/320,000已知Token；达到预算时停止，剩余缺题阻止通过。价格未知保持 `null`，产品和评分费用单列。R 必须另冻模式条件并准备新保留集，不能把D成绩改标为R。详细结果见[第7阶段报告](../docs/agent-evaluation-phase7-report.md)。

## V005/V007 最小 full_task 语义闭环

```powershell
conda run --no-capture-output -n nanobot python -m evals.fulltask_semantic --mode D --output tmp/fulltask-D-new
conda run --no-capture-output -n nanobot python -m evals.fulltask_semantic --mode D --control no_read --output tmp/fulltask-no-read-new
conda run --no-capture-output -n nanobot python -m evals.fulltask_audit tmp/fulltask-D-new --report tmp/fulltask-D-audit-new.json
```

此入口复用隔离的 `AgentLoop.process_direct`、产品 trace 和独立程序评分进程。候选包由该 trial 唯一实际 delivery 构造；V007 还需真实 `read_file` 返回预算所在行、成功工具调用及文件访问关联、冻结来源和前后文件哈希。读不到来源但猜中答案仍失败。程序硬失败优先，裁判不能推翻；gold 先审，双上下文初审及最多一次复判，引用、解析、争议、交付和预算门禁均参与资格推导。原 `archived_semantic` 限制与历史资格不变。

D 只能证明执行及评分协议。R 需先完成 D 正负控制和离线重放，再冻结预算，使用 `deepseek-v4-flash`。两题各三次采用三个独立双题运行，每次重新自检和审批 gold；每次产品每题最多8请求、16工具调用、32,000已知tokens、120秒，评分最多24 attempts、192,000保守预留tokens、600请求秒；每包每阶段至多3个逻辑调用、每调用2 attempts，SDK重试为0。未知usage阻止正式资格；价格未知金额保留null，不能据此作费用合格结论。

输出保留冻结源码、契约、gold、候选、原始事件、实际交付、文件状态、来源读取、程序verdict、裁判请求账本、费用及SHA索引。离线审计从这些证据重算资格，不调用Agent或模型，要求使用同版本评分源码。报告只能覆盖这两题和实际采集的边界，不能据此宣布120题完成或泛化能力通过。

## V088/V089/V091 邮件完整任务首批

```powershell
conda run --no-capture-output -n nanobot python -m evals.mail_task_semantic --mode D --case V089 --output tmp/mail-conflict-D-new
conda run --no-capture-output -n nanobot python -m evals.mail_task_semantic --mode D --case V089 --control latest_final --output tmp/mail-conflict-negative-new
conda run --no-capture-output -n nanobot python -m evals.mail_task_audit tmp/mail-conflict-D-new --report tmp/mail-conflict-audit-new.json
```

独立 `mail-full-task/1.0` profile 通过真实 `AgentLoop.process_direct`、持久 owner 认证及 `MailTurnExtension`，执行 MailRouter、MailAnswerService、真实检索和来源回读。合成资料沿用原题/已有邮件 corpus 映射，V089 INTERNALDATE 相差一天，V091 general.txt 是同封附件。固定文本检索、发送关闭、统一会话关闭，每题空的独立实例；没有固定 encoder，不代表 V085 通过。本 profile 没有开放 shell、消息、网络或文件工具，不能外推完整外发权限边界。

记录来源 UID/mail/document/part/locator、实际回答与引用、持久私有历史、事实和对象前后状态、业务 outbox，以及产品/裁判请求成本。正常 query/focus 状态和私有会话保存可写，业务资料及管理/外发行为另行校验。程序失败、缺来源、原文安全回退、引用异常、裁判争议、解析错误和预算耗尽均不能升级为完整成功。正式验收须同时要求资格门禁及独立离线审计通过。

首批 R 仅每题一次，以 D 正负控制/重放为前置，再冻结 R 预算；不据此宣称可靠性三次或全系统成绩。每题产品至多8 attempts/32,000已知tokens/180秒；独立评分至多24 attempts/192,000预留tokens/600请求秒，沿用每包阶段3逻辑调用、每调用2 attempts、SDK重试0。未知usage或环境缺失不能通过，价格未知金额为null。所有结果保留，产品缺陷单列修复而不改标准迎合输出。
