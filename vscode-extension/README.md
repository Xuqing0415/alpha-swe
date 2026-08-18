# Alpha-SWE VS Code 扩展（MVP）

在 VS Code 中选中代码，输入自然语言指令（如“重构这个函数”），由 Alpha-SWE
Agent 服务执行修改；任务完成后在侧栏打开工作区 `git diff` 查看变更。

## 前置条件

- 已启动 Alpha-SWE 服务：`python -m server.main`（FastAPI，监听 8000 端口，API 前缀 `/api/v1`）。
- 首次使用需要管理员创建 API Key 并换取访问令牌：

```powershell
# 创建 API Key（管理员）
curl -X POST http://127.0.0.1:8000/api/v1/api-keys -H "Content-Type: application/json" -d "{\"user_id\": 1}"
# 换取 Bearer Token
curl -X POST http://127.0.0.1:8000/api/v1/auth/token -H "Content-Type: application/json" -d "{\"api_key\": \"<key>\"}"
```

## 安装与调试

```bash
npm install
npm run compile
```

在 VS Code 中按 `F5`（Extension Development Host）加载本目录即可调试。

## 使用

1. 打开项目文件夹（作为任务 workspace）。
2. 选中代码，运行命令 **Alpha-SWE: 对选中代码执行自然语言指令**（快捷键 `Ctrl+Alt+S`）。
3. 输入指令并确认；通知栏显示任务进度（可取消）。
4. 完成后自动打开工作区 `git diff` 视图。

## 配置

| 键 | 默认值 | 说明 |
| --- | --- | --- |
| `alphaSwe.baseUrl` | `http://127.0.0.1:8000` | 服务地址 |
| `alphaSwe.apiKey` | `""` | Bearer Token；留空不携带 |
| `alphaSwe.timeout` | `1800` | 任务超时（秒） |

## 说明

- 当前为 MVP：仅支持“选中代码 + 指令”的提交、轮询、取消与 diff 展示；
  历史任务列表、SSE 实时进度、多工作区支持留待后续迭代。
- 服务端接口约定见 `server/main.py` 与 `docs/06-productization-service.md`。
