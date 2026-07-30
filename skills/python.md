# @skill(python)
# Python 开发技能模块

## Python 编码规范
- 遵循 PEP 8 代码风格
- 使用 type hints 标注类型
- 优先使用 dataclass 而非普通类
- 使用 f-string 格式化字符串
- 异常处理使用 try/except，避免裸 except

## 项目结构
- 使用 `if __name__ == "__main__"` 保护入口
- 配置文件使用 YAML 或 TOML
- 使用 logging 模块而非 print

## 常用操作
- 虚拟环境: `python -m venv .venv`
- 依赖管理: `pip install -r requirements.txt`
- 代码格式化: `black .` 或 `ruff format .`
- 类型检查: `mypy .`
- 测试: `pytest -v`