# 路线图

## v0.2 —— 多语言与工具增强（进行中）
- [x] 多语言符号/调用图提取（Java、Go、Rust、C/C++、C#、Ruby、PHP，正则回退）
- [x] 测试运行器扩展（Maven、Gradle、go test、cargo test、CTest）
- [x] 依赖清单识别（pom.xml、build.gradle、go.mod、Cargo.toml、CMakeLists.txt 等）
- [x] 数据库查询工具（SQLite/PostgreSQL/MySQL，只读默认 + 写操作确认）
- [x] 云 CLI 封装（aws/kubectl/docker，默认关闭，需显式授权）
- [x] VS Code 扩展 MVP（选中代码 + 自然语言指令）
- [ ] tree-sitter 精确解析默认开启（Java/Go/Rust/C++ grammar 打包）
- [ ] LSP 接入提升调用图精度

## v0.3 —— 插件生态与社区
- [ ] 插件/技能索引仓库与安装 CLI
- [ ] MCP 服务器开发教程与示例（数据库迁移助手、API 文档生成器）
- [ ] SWE-bench 评估流水线接入 CI（定时/手动触发）

## v0.4 —— 可观测性与体验
- [ ] Web 面板任务回放、决策轨迹可视化
- [ ] 多模态输入评估（截图理解 UI/架构图）

## 长期
- [ ] 多语言 SWE-bench 扩展（SWE-bench Multilingual 等）
- [ ] 团队级记忆与技能共享
