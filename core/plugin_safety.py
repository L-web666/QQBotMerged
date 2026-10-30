# -*- coding: utf-8 -*-
"""插件安全扫描（AST 静态分析）

插件是**任意 Python 代码、在主进程里执行**，没有沙箱（见 `plugins/README.md` 的警告）。
这里不改任何运行时行为，只在加载/查看插件时**静态**读一遍插件源码，把"值得你多看一眼"
的写法标出来，显示在插件管理页，帮你在装别人给的插件之前做判断。

设计原则：
- **只解析、不执行**：`ast.parse` 之后遍历语法树，绝不 import 插件代码；
- **只报事实 + 理由**：不说"这是恶意代码"，而是"这段代码能读写文件 / 能联网 / 会起进程"；
- **不阻断加载**：解析失败（源码有语法错误等）只返回空结果，插件本来就由加载器给出诊断。

命令行自查（不启动程序、不连 QQ）：

    python -m core.plugin_safety plugins
    python -m core.plugin_safety plugins/astrbot_plugin_dice
"""

import ast
import json
import os
import sys
from typing import Any, Dict, Iterable, List, Optional, Tuple

__all__ = ["RISK_RULES", "scan_source", "scan_file", "scan_plugin_dir", "summarize",
           "format_report"]

# 插件目录里真正要扫的文件（main.py 必扫，下划线开头的私有文件跳过）
SOURCES = ("main.py",)
MAX_FILES = 24
MAX_BYTES = 512 * 1024
MAX_STRINGS = 400

# `unknown` 是"没扫成 / 结果不完整"，**不能**排在 ok 后面当成安全
# （审计 SEC-07：扫描失败以前返回 ok，和"确认无风险"无法区分）。
LEVEL_ORDER = {"high": 0, "medium": 1, "unknown": 2, "info": 3, "ok": 4}

# 规则：命中任一 `imports` / `calls` / `attrs` / `names` / `strings` 即算命中
RISK_RULES: Tuple[Dict[str, Any], ...] = (
    {"id": "process", "level": "high", "label": "能启动进程/执行系统命令",
     "hint": "在机器人进程里起子进程或执行系统命令，等于把整台机器交给插件",
     "imports": ("subprocess", "pty", "multiprocessing"),
     "calls": ("os.system", "os.popen", "os.execv", "os.execvp", "os.spawnv",
               "os.startfile", "asyncio.create_subprocess_shell",
               "asyncio.create_subprocess_exec"),
     "attrs": ("subprocess.",)},
    {"id": "dynamic_code", "level": "high", "label": "动态执行代码",
     "hint": "eval/exec/compile 能把字符串当代码跑，静态扫描看不出它到底会做什么",
     "names": ("eval", "exec", "compile", "__import__"),
     "calls": ("importlib.import_module", "pickle.loads", "pickle.load",
               "marshal.loads", "yaml.unsafe_load", "yaml.load")},
    {"id": "secrets", "level": "high", "label": "会去读本程序的配置/密钥文件",
     "hint": "config.json 里有机器人 AppSecret 与 AI 密钥；插件没有理由读它",
     "strings": ("config.json", "app_secret", "cfut_", ".cloud_sync_state",
                 "plugins_disabled.json")},
    {"id": "credentials", "level": "info", "label": "用到了密钥类配置",
     "hint": "插件自己带 API Key 很常见；确认它只发给自己配置的服务地址即可",
     "strings": ("api_key", "api_token", "apikey")},
    {"id": "filesystem", "level": "medium", "label": "能读写文件/删除文件",
     "hint": "插件数据应放 data/plugin_data/<插件名>/；操作其它路径请自己确认",
     "imports": ("shutil", "glob", "tempfile"),
     "calls": ("os.remove", "os.unlink", "os.rmdir", "os.rename", "os.replace",
               "os.makedirs", "os.chmod", "os.chown", "shutil.rmtree", "shutil.move",
               "shutil.copy", "shutil.copy2", "open")},
    {"id": "network", "level": "medium", "label": "能联网",
     "hint": "插件可以往外发数据；第三方地址请确认可信",
     "imports": ("socket", "http.client", "ftplib", "smtplib", "telnetlib", "paramiko"),
     "calls": ("requests.get", "requests.post", "requests.request", "urllib.request.urlopen",
               "httpx.get", "httpx.post", "aiohttp.request")},
    {"id": "process_env", "level": "medium", "label": "会读环境变量/系统信息",
     "hint": "环境变量里可能有 QQBOT_* 覆盖的密钥",
     "attrs": ("os.environ", "os.getenv", "platform.node", "socket.gethostname",
               "getpass.getuser")},
    {"id": "thread", "level": "info", "label": "自己起线程/协程任务",
     "hint": "插件自起的线程不受本程序管理，终止插件时不一定停得掉",
     "imports": ("threading", "concurrent.futures", "asyncio"),
     "calls": ("threading.Thread", "threading.Timer",
               "asyncio.create_task", "asyncio.ensure_future",
               "asyncio.get_event_loop")},
    {"id": "time_control", "level": "info", "label": "会休眠/定时循环",
     "hint": "同步 sleep 会占住消息处理线程，建议用 async 写法",
     "calls": ("time.sleep",)},
    {"id": "reflection", "level": "info", "label": "用了反射/底层接口",
     "hint": "getattr/setattr/ctypes 会绕过静态检查，请自行确认用途",
     "imports": ("ctypes", "cffi"),
     "names": ("setattr", "delattr", "globals", "locals", "vars")},
)


class _Collector(ast.NodeVisitor):
    """把源码里"出现过的导入 / 调用 / 属性链 / 裸名字 / 字符串"收集起来。"""

    def __init__(self):
        self.imports: set = set()
        self.calls: set = set()
        self.attrs: set = set()
        self.names: set = set()
        self.strings: set = set()

    # ---- 导入 ----
    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            self.imports.add(alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        module = node.module or ""
        if module:
            self.imports.add(module)
            for alias in node.names:
                self.imports.add(f"{module}.{alias.name}")
        self.generic_visit(node)

    # ---- 名字 / 属性 / 调用 ----
    def visit_Name(self, node: ast.Name):
        self.names.add(node.id)

    def visit_Attribute(self, node: ast.Attribute):
        dotted = _dotted(node)
        if dotted:
            self.attrs.add(dotted)
            # 便于规则里写 `subprocess.` 这种前缀匹配
            parts = dotted.split(".")
            for index in range(1, len(parts)):
                self.attrs.add(".".join(parts[:index]) + ".")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call):
        dotted = _dotted(node.func)
        if dotted:
            self.calls.add(dotted)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant):
        value = node.value
        if isinstance(value, str) and 0 < len(value) <= 120 and len(self.strings) < MAX_STRINGS:
            self.strings.add(value)
        self.generic_visit(node)


def _dotted(node: ast.AST) -> str:
    """把 `os.path.join` 这种属性链还原成字符串（还原不出来就返回空串）。"""
    parts: List[str] = []
    current: Optional[ast.AST] = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    if isinstance(current, ast.Call):
        inner = _dotted(current.func)
        return f"{inner}()." + ".".join(reversed(parts)) if inner else ""
    return ""


def _hit_imports(imports: set, wanted: Iterable[str]) -> List[str]:
    out = []
    for name in wanted:
        for seen in imports:
            if seen == name or seen.startswith(name + "."):
                out.append(seen)
                break
    return out


def _hit_prefix(values: set, wanted: Iterable[str]) -> List[str]:
    out = []
    for name in wanted:
        for seen in values:
            if seen == name or seen.startswith(name + "."):
                out.append(seen)
                break
    return out


def _hit_strings(strings: set, wanted: Iterable[str]) -> List[str]:
    out = []
    for needle in wanted:
        if any(needle in text for text in strings):
            out.append(needle)
    return out


def scan_source(source: str, filename: str = "") -> List[Dict[str, Any]]:
    """静态扫一段插件源码，返回命中的风险项（不执行代码）。"""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as exc:
        # 解析失败**必须显式标出来**：以前直接返回空列表，于是"没扫成"看起来
        # 和"扫过且没问题"一模一样（审计 SEC-07 / NEW-04）。
        # 用 info 级别挂一条，`scan_plugin_dir` 据此把整体标成 incomplete。
        return [{"id": "parse", "level": "info", "label": "源码解析失败，未能扫描",
                 "hint": "该文件的写法无法静态分析，安全性未确认", "evidence": [str(exc)[:160]],
                 "parse_error": True, "file": filename or ""}]
    collector = _Collector()
    collector.visit(tree)

    findings: List[Dict[str, Any]] = []
    for rule in RISK_RULES:
        evidence: List[str] = []
        if rule.get("imports"):
            evidence.extend(_hit_imports(collector.imports, rule["imports"]))
        if rule.get("calls"):
            evidence.extend(_hit_prefix(collector.calls, rule["calls"]))
        if rule.get("attrs"):
            evidence.extend(_hit_prefix(collector.attrs, rule["attrs"]))
        if rule.get("names"):
            evidence.extend(name for name in rule["names"] if name in collector.names)
        if rule.get("strings"):
            evidence.extend(_hit_strings(collector.strings, rule["strings"]))
        if not evidence:
            continue
        findings.append({
            "id": rule["id"],
            "level": rule["level"],
            "label": rule["label"],
            "hint": rule["hint"],
            "file": os.path.basename(filename or ""),
            "evidence": sorted(set(evidence))[:6],
        })
    return findings


def scan_file(path: str) -> List[Dict[str, Any]]:
    """扫一个 .py 文件（读不了/太大就跳过，不抛异常）。"""
    try:
        if os.path.getsize(path) > MAX_BYTES:
            return []
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            source = handle.read()
    except OSError:
        return []
    return scan_source(source, path)


def scan_plugin_dir(dir_path: str) -> Dict[str, Any]:
    """扫一个插件目录（**递归子目录**）：返回 `{findings, files, level, counts}`。

    审计 NEW-04：以前只用单层 `os.listdir` + `isfile()` 判断，**子目录被整体跳过** ——
    插件把恶意逻辑放进 `helpers/evil.py` 或任意子包里，就完全不在扫描范围内。
    现在递归遍历所有 `.py`（含子包与下划线辅助模块），并记录"是否扫全"：
    超过文件数/大小上限、解析失败、目录不可读，都会标成 `incomplete`，
    由上层显示成"扫描未完成"而不是"安全"。
    """
    findings: List[Dict[str, Any]] = []
    files: List[str] = []
    incomplete: List[str] = []
    if not os.path.isdir(dir_path):
        return summarize(findings, files, incomplete=["目录不存在"])
    candidates: List[tuple] = []
    try:
        for current, dirnames, filenames in os.walk(dir_path):
            # 跳过明显的无关目录，避免把 .git / 虚拟环境扫进来
            dirnames[:] = sorted(d for d in dirnames
                                 if d not in (".git", "__pycache__", ".venv", "venv",
                                              "node_modules"))
            for name in sorted(filenames):
                if not name.endswith(".py"):
                    continue
                full = os.path.join(current, name)
                rel = os.path.relpath(full, dir_path).replace("\\", "/")
                candidates.append((rel, full))
    except OSError as exc:
        return summarize(findings, files, incomplete=[f"目录不可读：{exc}"])
    # main.py 优先扫，保证"入口文件"一定在扫描额度内
    candidates.sort(key=lambda item: (item[0] != "main.py", item[0]))
    scanned = 0
    for rel, full in candidates:
        if scanned >= MAX_FILES:
            incomplete.append(f"文件数超过上限 {MAX_FILES}，其余未扫描")
            break
        try:
            if os.path.getsize(full) > MAX_BYTES:
                incomplete.append(f"{rel} 超过 {MAX_BYTES // 1024}KB，未扫描")
                continue
        except OSError:
            incomplete.append(f"{rel} 无法读取")
            continue
        scanned += 1
        files.append(rel)
        file_findings = scan_file(full)
        findings.extend(file_findings)
        if any(item.get("parse_error") for item in file_findings):
            incomplete.append(f"{rel} 解析失败，结果不完整")
    return summarize(findings, files, incomplete=incomplete)


def scan_container(root: str) -> Dict[str, Any]:
    """扫"插件根目录"（`plugins/`）：对里面每个插件子目录各扫一遍再合并。"""
    findings: List[Dict[str, Any]] = []
    files: List[str] = []
    if not os.path.isdir(root):
        return summarize(findings, files)
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return summarize(findings, files)
    for name in entries:
        if name.startswith((".", "_")):
            continue
        sub = os.path.join(root, name)
        if not os.path.isdir(sub):
            continue
        result = scan_plugin_dir(sub)
        for item in result["findings"]:
            merged = dict(item)
            merged["plugin"] = name
            merged["file"] = f"{name}/{item.get('file') or ''}"
            findings.append(merged)
        files.extend(f"{name}/{item}" for item in result["files"])
    return summarize(findings, files)


def summarize(findings: List[Dict[str, Any]], files: Optional[List[str]] = None,
              incomplete: Optional[List[str]] = None) -> Dict[str, Any]:
    """把命中的风险项压成给网页看的一行摘要。

    `incomplete` 非空表示"没扫全"（超上限/解析失败/目录不可读）：
    这时即使没有高风险项也**不能**报 `ok`，而是报 `unknown`，
    并在 summary 里写清原因（审计 SEC-07 / NEW-04）。
    """
    counts: Dict[str, int] = {}
    for item in findings:
        counts[item["level"]] = counts.get(item["level"], 0) + 1
    level = "ok"
    for candidate in ("high", "medium", "info"):
        if counts.get(candidate):
            level = candidate
            break
    notes = [str(item) for item in (incomplete or []) if item]
    if notes and level in ("ok", "info"):
        # 没有高风险项、但没扫全 → 结论必须是"未确认"
        level = "unknown"
    summary = _summary_text(level, counts)
    if notes:
        summary += "；未扫全：" + "、".join(notes[:4]) + ("…" if len(notes) > 4 else "")
    return {
        "level": level,                     # high / medium / unknown / info / ok
        "counts": counts,
        "findings": findings,
        "files": list(files or []),
        "summary": summary,
        "incomplete": bool(notes),
    }


def _summary_text(level: str, counts: Dict[str, int]) -> str:
    if level == "unknown":
        return "安全扫描未完成（安全性未确认）"
    if level == "ok":
        return "没扫到需要留意的写法"
    parts = []
    for key, label in (("high", "高风险"), ("medium", "中风险"), ("info", "提示")):
        if counts.get(key):
            parts.append(f"{label} {counts[key]} 项")
    return "、".join(parts)


def format_report(result: Dict[str, Any], title: str = "") -> str:
    """给命令行看的纯文本报告。"""
    lines = []
    if title:
        lines.append(title)
    lines.append(f"  结论：{result.get('summary') or ''}")
    if result.get("files"):
        lines.append("  扫描文件：" + "、".join(result["files"]))
    for item in result.get("findings") or []:
        evidence = "、".join(item.get("evidence") or [])
        lines.append(f"  [{item['level']}] {item['label']}（{item['file']}）")
        lines.append(f"        命中：{evidence}")
        lines.append(f"        说明：{item['hint']}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    targets = list(argv if argv is not None else sys.argv[1:])
    if not targets:
        default_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   "plugins")
        targets = [default_dir]
    exit_code = 0
    for target in targets:
        if os.path.isdir(target):
            # 既支持单个插件目录，也支持 plugins/ 这种"装着很多插件"的根目录
            has_py = any(name.endswith(".py") for name in (os.listdir(target) or []))
            result = scan_plugin_dir(target) if has_py else scan_container(target)
            print(format_report(result, f"目录 {target}"))
            # 只有"高风险"才算需要处理的（中风险/提示在插件里很常见，只做参考）
            exit_code = max(exit_code, 1 if result["level"] == "high" else 0)
        elif os.path.isfile(target):
            findings = scan_file(target)
            result = summarize(findings, [os.path.basename(target)])
            print(format_report(result, f"文件 {target}"))
            exit_code = max(exit_code, 1 if result["level"] == "high" else 0)
        else:
            print(f"找不到：{target}")
            exit_code = 2
    print(json.dumps({"ok": exit_code == 0}, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
