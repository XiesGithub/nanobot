# 独立 Agent 教学示例

`mini_agent/` 是新写的标准库教学实现，不是 nanobot 源码摘录。只依赖 Python 3.11+，可将整个目录复制到任意位置运行。默认 ScriptedProvider 离线、确定性，无网络、真实 LLM 或密钥读取。

进入复制后的 `mini_agent` 目录：

```text
conda run -n nanobot python app.py
conda run -n nanobot python -m unittest discover -s . -v
```

预期答案为 `Result: 5`，完整 trace 和全部七个文件源码均嵌入[第 10 章](../chapters/10-build-your-agent/index.html)。测试应输出 `Ran 12 tests` / `OK`。

默认使用临时存储目录；保留会话请加 `--state-dir demo-state --session alice`。此存储只支持单写入者，未实现并发锁、鉴权或崩溃中的回合恢复。只开放一个短小的纯函数加法工具，没有 shell、网络、文件工具或生产沙箱。

手册维护验证（仓库根目录）：

```text
conda run -n nanobot python study/examples/verify_example.py
```

该检查比较页面嵌入源码和文件、验证章节内链接，并把示例复制到临时目录启动独立进程，检查精确输出、全部回归测试、跨进程持久化与隔离。子进程使用同一个 Conda Python 解释器。
