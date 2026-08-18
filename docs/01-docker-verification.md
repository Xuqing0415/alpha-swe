# 方向一 · 阶段 1：真实 Docker 沙箱验证报告

- 验证日期：2026-08-18
- 环境：Windows 11（Docker Desktop / WSL2 backend）
- Docker Engine：29.6.1（Linux）
- 基础镜像：`alphaswe/dev:latest`（构建脚本见 `docker/Dockerfile`：python 3.11.16 + git 2.47.3 + node v20.19.2 + curl + vim + jq）
- 验证套件：`tests/test_docker_real.py`（18 项，真实 daemon）；`tests/test_docker_sandbox.py`（13 项，fake client 离线，回归保护）
- 运行方式：真实 Docker 环境直接 `python -X utf8 -m pytest tests/test_docker_real.py -q`；daemon 不可达时整模块自动 skip（`@pytest.mark.docker`）

## 一、验证结果总览

| 验证点 | 结果 | 说明 |
|---|---|---|
| A. 容器生命周期 | ✅ 通过 | 创建/exec/停止/删除、异常退出码捕获、超时强杀+自动重启、稳定容器名 |
| B. 网络策略 | ✅ 通过 | `none` 模式外网不可达；`bridge` 模式可达；实际 NetworkMode 经 `docker inspect` 交叉验证 |
| C. 资源限制 | ✅ 通过 | 96MB 内存 OOM kill（exit 137）；NanoCpus 写入 HostConfig；stats() 采样 |
| D. 文件挂载与隔离 | ✅ 通过 | 工作区 bind 挂载 rw 双向同步；`ro` 挂载只读；路径穿越（含 `%2f` 编码）拦截；tmpfs `/tmp` 可写；重启后文件持久 |
| E. 快照与回滚 | ✅ 通过 | commit 快照→修改→回滚重建恢复；工作区 bind 文件回滚后保留；快照镜像自动清理（max_snapshots） |
| 端到端（AgentLoop） | ✅ 通过 | terminal/file 工具路由进真实容器、任务前快照、close 清理 |

31 项（13 fake + 18 real）全部通过。

## 二、发现并修复的真实环境问题

### 1. exec_run 不支持 shell 语法（严重，已修复）

- **现象**：`docker exec` 不经过 shell（docker-py 用 shlex 拆分字符串命令）。`mkdir -p /opt/mark && echo v1 > /opt/mark/version` 被拆成参数执行，`mkdir` 把 `&&`、`echo`、`>`、`/opt/mark/version` 都当成目录名创建——产生错误目录、命令静默"假成功"。
- **影响**：Agent 在容器内执行管道/重定向/`&&`/`||` 全部失效；部分离线测试因断言只检查 stdout 片段而成为假阳性。
- **修复**：`DockerSandbox._shell_cmd()` 把字符串命令包装为 `["/bin/sh", "-c", cmd]`（`agent/sandbox/docker_sandbox.py`）；列表命令原样透传。fake 客户端同步支持拆包。
- **验证**：真实容器内 `echo hello | tr a-z A-Z && ... > ... && cat`、`grep -n`、`pip install` 全部正确执行。

### 2. 镜像 CMD 导致容器启动即退出（环境问题，已修复）

- **现象**：基础镜像 `CMD ["/bin/bash"]` 无 TTY 启动即退出，所有 `docker exec` 报 `container is not running`。
- **修复**：`docker/Dockerfile` 改为 `CMD ["/bin/bash", "-c", "sleep infinity"]`，容器保持运行。

### 3. tmpfs 参数格式错误（已修复）

- **现象**：`tmpfs={path: {"mode": "0777"}}` 被 daemon 拒绝（`HostConfig.Tmpfs of type string`）。
- **修复**：改为 docker API 要求的 `{path: "rw,size=64m"}` 字符串格式。

### 4. write_file 不自动创建父目录（已修复）

- **现象**：`write_file("proj/marker.txt")` 时容器内 `/workspace/proj` 不存在，`put_archive` 失败。
- **修复**：`write_file` 先 `mkdir -p` 父目录，与本地 FileIOTool 行为一致。

### 5. 容器路径穿越未覆盖 URL 编码（已修复）

- **现象**：`..%2f..%2fetc%2fpasswd` 未被 `..` 段检查识别。
- **修复**：`_container_path` 先解码 `%2e`/`%2f` 再检查 `..` 段。

### 6. 超时强杀后容器不可用（已修复）

- **现象**：命令超时 `container.kill()` 后容器死亡，`running` 仍为 True，后续 exec 全部报 `container is not running`。
- **修复**：新增 `restart_after_timeout` 配置（默认 True），超时 kill 后自动 `container.restart()`；重启失败则标记容器已死，由下次 `start()` 重建。

### 7. 快照镜像无限累积（已修复）

- **现象**：每次任务前 commit 一个快照镜像，长时间任务累积占用磁盘。
- **修复**：新增 `max_snapshots` 配置（默认 5）+ `cleanup_snapshots()`，`snapshot()` 后自动清理最旧镜像；`snapshot_images()` 可观测。

## 三、关键设计与限制（真实环境结论）

- **快照 = 环境状态，不是项目文件**：工作区是 bind 挂载，`docker commit` 不捕获挂载卷内容；回滚恢复的是容器根文件系统（如已安装的依赖），项目文件回滚依赖 FileAuditStore 审计回滚与 git（见 `agent/sandbox/audit.py`、`GitTool`）。
- **read_only_root=True 的取舍**：根只读 + `/tmp` tmpfs 保证安全与临时文件可用；但 `pip install` 写入 `/usr/local` 会失败。依赖安装类任务应配置 `read_only_root: false`（或使用虚拟环境装入工作区）。
- **网络策略分层**：Docker 层只做 `none`/`bridge` 隔离；白名单/假网络在本地 `SandboxPolicy` 工具层生效（见 `tests/test_sandbox_security.py`），两层叠加使用。
- **性能**：单会话单容器复用（非每次 exec 重建）；快照 commit 约秒级，`max_snapshots` 控制磁盘增长。

## 四、后续建议

1. 容器池化：高频并发场景可预创建 N 个容器复用（当前单会话单容器已够用）。
2. Windows 挂载性能：Docker Desktop 跨 WSL2 文件系统同步较慢，大项目建议在容器内 git clone 而非挂载整个仓库。
3. CI 接入：GitHub Actions 提供 `services: docker` 或原生 docker runner，`tests/test_docker_real.py` 会自动运行；本地无 Docker 时自动跳过。
