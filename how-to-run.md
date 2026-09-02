# nanobot 本地启动方法

项目已经配置好 `nanobot` Conda 环境、DeepSeek API 和 WebUI。日常启动只需执行以下步骤。

## 1. 启动项目

打开 PowerShell 或 Anaconda Prompt，执行：

```powershell
conda activate nanobot
Set-Location 'D:\硕士资料\Project\nanobot'
python -m nanobot.cli.commands gateway --verbose
```

看到 WebSocket 和健康检查端点启动成功后，保持该终端窗口运行。

## 2. 打开 WebUI

浏览器访问：

- WebUI：<http://127.0.0.1:8765>
- Gateway 健康检查：<http://127.0.0.1:18790/health>

健康检查返回 `{"status": "ok"}` 即表示服务正常。

## 3. 停止项目

回到运行 gateway 的终端，按 `Ctrl+C` 即可停止。

## WebUI 代码发生变化时

只有修改了 `webui` 目录中的前端代码后，才需要重新构建：

```powershell
conda activate nanobot
Set-Location 'D:\硕士资料\Project\nanobot\webui'
npm run build
```

构建完成后，重新启动 gateway。
