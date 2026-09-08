"""跨平台 shell 命令生成器

统一生成当前平台可用的命令：
- Windows: PowerShell 语法（配合 tools/terminal.py 中的 PowerShell 执行器）
- Unix/Linux: bash 语法（grep/find/head）
"""
import os

IS_WINDOWS = os.name == "nt"


def search_console_log(exclude_node_modules: bool = True) -> str:
    """搜索 .ts/.js 文件中的 console.log 调用（最多 50 条）

    默认跳过 node_modules 与 dist 编译产物。
    """
    if IS_WINDOWS:
        skip = "node_modules|dist" if exclude_node_modules else "dist"
        return (
            "Get-ChildItem -Path . -Recurse -File -Include *.ts,*.js "
            f"| Where-Object {{ $_.FullName -notmatch '{skip}' }} "
            "| Select-String -Pattern 'console\\.log' | Select-Object -First 50"
        )
    exclude = "--exclude-dir=node_modules --exclude-dir=dist " if exclude_node_modules else "--exclude-dir=dist "
    return f"grep -rn 'console\\.log' . {exclude}--include='*.ts' --include='*.js' 2>/dev/null | head -50"


def list_dir() -> str:
    """列出当前目录内容"""
    if IS_WINDOWS:
        return "Get-ChildItem -Force | Format-Table -AutoSize"
    return "ls -la"


def find_files(extensions=(".py", ".ts", ".js"), limit: int = 20) -> str:
    """按扩展名递归查找文件"""
    if IS_WINDOWS:
        exts = ",".join(f"'{e}'" for e in extensions)
        return (
            f"Get-ChildItem -Path . -Recurse -File | "
            f"Where-Object {{ $_.Extension -in {exts} }} | "
            f"Select-Object -First {limit} -ExpandProperty FullName"
        )
    names = " ".join(f"-o -name '*{e}'" for e in extensions)
    return f"find . -type f -name '*{extensions[0]}' {names} | head -{limit}"


def list_all_files(limit: int = 100) -> str:
    """递归列出所有文件"""
    if IS_WINDOWS:
        return f"Get-ChildItem -Path . -Recurse -File | Select-Object -First {limit} -ExpandProperty FullName"
    return f"find . -type f 2>/dev/null | head -{limit}"


def search_file(filename: str) -> str:
    """按文件名递归搜索"""
    if IS_WINDOWS:
        return (
            f"Get-ChildItem -Path . -Recurse -File -Filter {filename} | "
            "Select-Object -ExpandProperty FullName"
        )
    return f"find . -name '{filename}' -type f 2>/dev/null"


def find_readable_files(limit: int = 20) -> str:
    """查找可读文件（权限不足时的替代方案）"""
    if IS_WINDOWS:
        return list_all_files(limit)
    return f"find . -type f -readable 2>/dev/null | head -{limit}"

