#!/usr/bin/env python3
"""
Локальный агент код-ревью на базе LM Studio (OpenAI-совместимый API).

Пайплайн:
  1. Находит изменённые файлы в git-репозитории.
     Режимы: uncommitted (staged+unstaged), --staged, --last, --base <ref>.
  2. Собирает контекст ВСЕГО проекта:
        - карта репозитория (дерево файлов);
        - связанные файлы: импорты/require для TS/JS, namespace/using для C#,
          ссылки на объекты для SQL, плюс эвристический поиск по всему коду.
  3. Для каждого изменённого файла просит локальную LLM вернуть строгий JSON
     с замечаниями.
  4. Механически валидирует ответ: JSON-схема + привязка строк и фрагментов
     к реальному коду (grounding). Непривязанные "галлюцинации" отбрасываются.
  5. Пишет Markdown-отчёт рядом с этим скриптом.

Примеры:
    python agent_system.py "C:\\path\\to\\repo"
    python agent_system.py . --base main
    python agent_system.py . --staged
    python agent_system.py . --last
    python agent_system.py . --no-llm       # git + контекст без вызова модели
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

SCRIPT_DIR = Path(__file__).resolve().parent

LM_STUDIO_BASE = os.environ.get("LM_STUDIO_URL", "http://localhost:1234/v1")

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "nit": 4}
ALLOWED_SEVERITY = set(SEVERITY_ORDER)

LANG_BY_EXT = {
    ".cs": "C#",
    ".csproj": "MSBuild",
    ".props": "MSBuild",
    ".targets": "MSBuild",
    ".sln": "Solution",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".sql": "SQL",
    ".py": "Python",
    ".json": "JSON",
    ".yml": "YAML",
    ".yaml": "YAML",
    ".xml": "XML",
    ".config": "XML",
    ".md": "Markdown",
    ".cshtml": "Razor",
    ".razor": "Razor",
    ".ps1": "PowerShell",
    ".sh": "Shell",
}

CODE_EXT = set(LANG_BY_EXT)

EXCLUDE_DIRS = {
    ".git", "node_modules", "bin", "obj", "dist", "build", ".vs", ".idea",
    ".vscode", "packages", "coverage", ".next", "out", "vendor", "__pycache__",
    ".venv", "venv", ".mypy_cache", ".pytest_cache", "TestResults", "$RECYCLE.BIN",
}

MAX_FILE_BYTES = 1_000_000
MAX_SCAN_FILES = 4000
MAX_RELATED_FILES = 5
MAX_RELATED_CHARS = 6000
MAX_MAP_ENTRIES = 600

SYSTEM_PROMPT = (
    "Ты — опытный senior-инженер, который делает строгое, но по делу code review.\n"
    "Ты видишь ТОЛЬКО предоставленный diff, полный текст изменённого файла и связанный "
    "контекст проекта. Не выдумывай код, которого нет.\n"
    "Каждое замечание обязано опираться на конкретную строку нового файла.\n"
    "В поле evidence вставь ТОЧНУЮ подстроку кода из нового файла или diff (1-3 строки), "
    "которая подтверждает замечание. Если подтверждения нет — не создавай замечание.\n"
    "Не комментируй то, что не менялось, если это не причина проблемы в изменении.\n"
    "Отвечай СТРОГО валидным JSON, без markdown и без пояснений вокруг."
)


def read_text(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return ""


def run_git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    cmd = ["git", "-c", "core.quotepath=false", "-C", str(repo), *args]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def git_output(repo: Path, *args: str) -> str:
    p = run_git(repo, *args)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip())
    return p.stdout


def git_ok(repo: Path, *args: str) -> bool:
    return run_git(repo, *args).returncode == 0


def detect_model() -> str:
    try:
        r = requests.get(f"{LM_STUDIO_BASE}/models", timeout=10)
        r.raise_for_status()
        for m in r.json().get("data", []):
            mid = m.get("id", "")
            if "embed" not in mid.lower():
                return mid
    except Exception:
        pass
    return os.environ.get("LM_MODEL", "local-model")


def chat(messages, model, temperature=0.1, max_tokens=1800, timeout=600):
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    r = requests.post(f"{LM_STUDIO_BASE}/chat/completions", json=payload, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    content = data["choices"][0]["message"]["content"] or ""
    return content, data.get("usage", {})


def collect_index(repo: Path):
    index = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith(".")]
        for name in files:
            if Path(name).suffix.lower() in CODE_EXT:
                full = Path(root) / name
                try:
                    rel = full.relative_to(repo).as_posix()
                except ValueError:
                    continue
                index.append(rel)
                if len(index) >= MAX_SCAN_FILES:
                    return index
    return sorted(index)


def build_repo_map(index) -> str:
    if not index:
        return "(пусто)"
    by_dir = {}
    for rel in index:
        parent = str(Path(rel).parent)
        parent = "." if parent == "." else parent
        by_dir.setdefault(parent, []).append(Path(rel).name)
    lines = []
    for i, d in enumerate(sorted(by_dir)):
        if i >= MAX_MAP_ENTRIES:
            lines.append(f"... и ещё {len(by_dir) - i} каталогов")
            break
        names = sorted(by_dir[d])[:20]
        suffix = "" if len(by_dir[d]) <= 20 else f" (+{len(by_dir[d]) - 20})"
        lines.append(f"{d}/: {', '.join(names)}{suffix}")
    return "\n".join(lines)


def resolve_review_target(args):
    if args.last:
        return ["HEAD~1...HEAD"], "HEAD~1", "изменения последнего коммита"
    if args.base:
        return [f"{args.base}...HEAD"], args.base, f"изменения ветки относительно {args.base}"
    if args.staged:
        return ["--cached"], "HEAD", "проиндексированные изменения (staged)"
    return ["HEAD"], "HEAD", "незакоммиченные изменения (staged + unstaged)"


def parse_name_status(text):
    entries = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        if status[0] in ("R", "C") and len(parts) >= 3:
            entries.append((status[0], parts[1], parts[2]))
        elif len(parts) >= 2:
            entries.append((status[0], parts[1], parts[1]))
    return entries


def changed_files(repo: Path, diff_args, base_ref, include_untracked=True):
    out = git_output(repo, "diff", "--name-status", "-M", *diff_args)
    entries = parse_name_status(out)

    if include_untracked and diff_args == ["HEAD"]:
        untracked = git_output(repo, "ls-files", "--others", "--exclude-standard")
        for path in untracked.splitlines():
            path = path.strip()
            if path:
                entries.append(("A", path, path))

    result = []
    for status, old, new in entries:
        if Path(new).suffix.lower() not in CODE_EXT and status != "D":
            continue
        diff_text = ""
        try:
            if old != new:
                diff_text = git_output(repo, "diff", "-M", *diff_args, "--", old, new)
            else:
                diff_text = git_output(repo, "diff", "-M", *diff_args, "--", new)
        except RuntimeError:
            pass

        old_text = ""
        if status != "A":
            try:
                old_text = git_output(repo, "show", f"{base_ref}:{old}")
            except RuntimeError:
                old_text = ""

        new_path = repo / new
        new_text = read_text(new_path) if new_path.exists() else ""

        result.append({
            "status": status,
            "old_path": old,
            "path": new,
            "diff": diff_text,
            "old_text": old_text,
            "new_text": new_text,
        })
    return result


def resolve_js_import(importer: Path, spec: str, repo: Path, index_set):
    base = (importer.parent / spec)
    candidates = []
    if base.suffix:
        candidates.append(base)
    else:
        for ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
            candidates.append(Path(str(base) + ext))
        for ext in (".ts", ".tsx", ".js", ".jsx"):
            candidates.append(base / f"index{ext}")
    for c in candidates:
        try:
            rel = c.relative_to(repo).as_posix()
        except ValueError:
            continue
        if rel in index_set:
            return rel
    return None


def find_related_files(repo: Path, path: str, index, cache: dict):
    lang = LANG_BY_EXT.get(Path(path).suffix.lower(), "")
    index_set = set(index)
    candidates = []

    text = cache.get(path)
    if text is None:
        text = read_text(repo / path)
        cache[path] = text

    if lang in ("TypeScript", "JavaScript"):
        specs = re.findall(
            r"""(?:from|import|require)\s*\(?\s*['"](\.[^'"]+)['"]""", text)
        for spec in specs:
            rel = resolve_js_import(repo / path, spec, repo, index_set)
            if rel and rel != path and rel not in candidates:
                candidates.append(rel)

    elif lang == "C#":
        namespaces = re.findall(r"^\s*using\s+([A-Za-z0-9_.]+)\s*;", text, re.M)
        stems = set(re.findall(r"\b([A-Z][A-Za-z0-9_]{2,})\b", text))
        for f in index:
            if f == path or f in candidates:
                continue
            ftext = cache.get(f)
            if ftext is None:
                ftext = read_text(repo / f)
                cache[f] = ftext
            if not ftext:
                continue
            if any(f"namespace {ns}" in ftext for ns in namespaces) or \
               Path(f).stem in stems:
                candidates.append(f)
            if len(candidates) >= MAX_RELATED_FILES * 3:
                break

    elif lang == "SQL":
        objects = re.findall(
            r"\b(?:FROM|JOIN|EXEC(?:UTE)?|UPDATE|INSERT\s+INTO|DELETE\s+FROM|REFERENCES)"
            r"\s+([A-Za-z0-9_.\[\]]+)", text, re.I)
        tokens = {o.split(".")[-1].strip("[]") for o in objects}
        tokens |= {Path(path).stem}
        for f in index:
            if f == path or f in candidates:
                continue
            ftext = cache.get(f)
            if ftext is None:
                ftext = read_text(repo / f)
                cache[f] = ftext
            if any(tok and re.search(rf"\b{re.escape(tok)}\b", ftext, re.I) for tok in tokens):
                candidates.append(f)
            if len(candidates) >= MAX_RELATED_FILES * 3:
                break

    if len(candidates) < MAX_RELATED_FILES:
        stem = Path(path).stem
        if len(stem) >= 3:
            for f in index:
                if f == path or f in candidates:
                    continue
                ftext = cache.get(f)
                if ftext is None:
                    ftext = read_text(repo / f)
                    cache[f] = ftext
                if re.search(rf"\b{re.escape(stem)}\b", ftext):
                    candidates.append(f)
                if len(candidates) >= MAX_RELATED_FILES:
                    break

    trimmed = []
    for rel in candidates[:MAX_RELATED_FILES]:
        body = cache.get(rel)
        if body is None:
            body = read_text(repo / rel)
            cache[rel] = body
        if not body:
            continue
        if len(body) > MAX_RELATED_CHARS:
            body = body[:MAX_RELATED_CHARS] + "\n... [усечено]"
        trimmed.append((rel, body))
    return trimmed


def mechanical_checks(repo: Path, diff_args) -> str:
    lines = []
    try:
        out = git_output(repo, "diff", "--check", *diff_args)
        lines.append("git diff --check: OK (пробельные ошибки/маркеры конфликтов не найдены)"
                     if not out.strip() else f"git diff --check:\n{out.strip()}")
    except RuntimeError as e:
        lines.append(f"git diff --check: не удалось ({e})")
    return "\n".join(lines)


def extract_json(text: str):
    text = re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", text, flags=re.S | re.I)
    text = re.sub(r"<\|[^>]*\|>", "", text)
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("в ответе модели нет JSON-объекта")
    return json.loads(text[start:end + 1])


def normalize_ws(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def ground_findings(findings, new_text, old_text, diff_text):
    new_lines = new_text.splitlines()
    old_lines = old_text.splitlines()
    haystack = normalize_ws(diff_text) + normalize_ws(new_text)
    accepted, rejected = [], []

    for f in findings:
        if not isinstance(f, dict):
            rejected.append({"finding": f, "reason": "не объект"})
            continue
        sev = str(f.get("severity", "medium")).lower().strip()
        if sev not in ALLOWED_SEVERITY:
            sev = "medium"
        f["severity"] = sev
        try:
            line = int(f.get("line", 0))
        except (TypeError, ValueError):
            line = 0
        f["line"] = line

        evidence = str(f.get("evidence", "") or "")
        line_ok = 1 <= line <= len(new_lines)
        deleted_ok = (not new_lines and old_text and 1 <= line <= len(old_lines))
        ev_ok = bool(evidence.strip()) and normalize_ws(evidence) in haystack

        if not line_ok and not deleted_ok:
            rejected.append({"finding": f, "reason": f"строки {line} нет в файле"})
            continue
        if evidence.strip() and not ev_ok:
            rejected.append({"finding": f, "reason": "evidence не найден в коде/diff"})
            continue
        if not evidence.strip():
            f["evidence"] = new_lines[line - 1].strip() if line_ok else ""
        f["grounded"] = True
        accepted.append(f)

    return accepted, rejected


def build_user_prompt(target, repo_map, related, max_chars, description=""):
    parts = [
        f"ФАЙЛ: {target['path']} (статус {target['status']}, язык "
        f"{LANG_BY_EXT.get(Path(target['path']).suffix.lower(), '?')})",
        "",
        "КАРТА РЕПОЗИТОРИЯ (структура всего проекта):",
        repo_map,
    ]
    if description.strip():
        parts += [
            "",
            "ОПИСАНИЕ ИЗМЕНЕНИЙ (что планировалось сделать):",
            description.strip(),
        ]
    parts += [
        "",
        "GIT DIFF:",
        "GIT DIFF:",
        target["diff"] or "(нет текстового diff, новый файл)",
        "",
        "ПОЛНЫЙ ТЕКСТ НОВОГО ФАЙЛА:",
        target["new_text"] or "(файл удалён)",
    ]
    if related:
        parts.append("")
        parts.append("СВЯЗАННЫЕ ФАЙЛЫ (контекст, НЕ рецензируй их отдельно):")
        for rel, body in related:
            parts.append(f"--- {rel} ---\n{body}")
    parts += [
        "",
        "ЗАДАЧА: найди реальные проблемы именно в изменениях этого файла "
        "(баги, ошибки логики, null/типизация, безопасность, async, "
        "производительность, SQL-инъекции и т.п.). Учитывай контекст связанных файлов.",
        "",
        "Верни JSON строго такого вида:",
        "{",
        '  "findings": [',
        "    {",
        '      "severity": "critical|high|medium|low|nit",',
        '      "category": "bug|security|correctness|performance|style|maintainability",',
        '      "line": 42,',
        '      "title": "кратко",',
        '      "comment": "что не так и почему",',
        '      "suggestion": "как исправить",',
        '      "evidence": "точная подстрока из нового файла/diff"',
        "    }",
        "  ]",
        "}",
        "Если проблем нет — верни {\"findings\": []}. Нумерация строк — по новому файлу.",
    ]
    text = "\n".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n... [контекст усечён по лимиту]"
    return text


def review_file(target, repo_map, related, model, max_chars, retries=2, description=""):
    user = build_user_prompt(target, repo_map, related, max_chars, description)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]
    last_err = ""
    for attempt in range(retries + 1):
        try:
            content, usage = chat(messages, model)
            data = extract_json(content)
            findings = data.get("findings", [])
            if not isinstance(findings, list):
                raise ValueError('поле "findings" не массив')
            return findings, usage, ""
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            messages.append({
                "role": "user",
                "content": f"Ответ не распарсился ({last_err}). Верни ТОЛЬКО валидный JSON по схеме.",
            })
    return [], {}, last_err


def severity_badge(sev):
    return {
        "critical": "CRITICAL", "high": "HIGH", "medium": "MEDIUM",
        "low": "LOW", "nit": "NIT",
    }.get(sev, sev.upper())


def write_report(path, data):
    lines = []
    lines.append("# Отчёт код-ревью (локальная LLM)\n")
    lines.append(f"- Репозиторий: `{data['repo']}`")
    lines.append(f"- Режим: {data['mode']}")
    lines.append(f"- Модель: `{data['model']}`")
    lines.append(f"- Дата: {data['timestamp']}")
    lines.append(f"- Длительность: {data['duration']:.1f} с")
    if data.get("description"):
        desc = data["description"].strip().replace("\n", " ")
        if len(desc) > 400:
            desc = desc[:400] + "..."
        lines.append(f"- Описание изменений: {desc}")
    lines.append(f"- Проверено файлов: {data['files_reviewed']} из {data['files_total']}")
    lines.append(f"- Замечаний подтверждено: {data['accepted_count']} | "
                 f"отклонено grounding-валидатором: {data['rejected_count']}")
    if data.get("tokens"):
        lines.append(f"- Токены (usage): {data['tokens']}")
    lines.append("")

    lines.append("## Механические проверки\n")
    lines.append("```")
    lines.append(data["mech"])
    lines.append("```\n")

    lines.append("## Сводка по серьёзности\n")
    if data["severity_counts"]:
        for sev in sorted(data["severity_counts"], key=lambda s: SEVERITY_ORDER.get(s, 9)):
            lines.append(f"- {severity_badge(sev)}: {data['severity_counts'][sev]}")
    else:
        lines.append("- замечаний нет")
    lines.append("")

    lines.append("## Замечания\n")
    if not data["by_file"]:
        lines.append("_Подтверждённых замечаний нет._\n")
    for fpath in sorted(data["by_file"]):
        lines.append(f"### `{fpath}`\n")
        for f in sorted(data["by_file"][fpath],
                        key=lambda x: (SEVERITY_ORDER.get(x.get("severity"), 9), x.get("line", 0))):
            lines.append(f"- **[{severity_badge(f.get('severity'))}]** "
                         f"(строка {f.get('line')}) {f.get('title', '')}")
            if f.get("comment"):
                lines.append(f"  {f['comment']}")
            if f.get("suggestion"):
                lines.append(f"  - Предложение: {f['suggestion']}")
            if f.get("evidence"):
                lines.append(f"  - Доказательство: `{f['evidence']}`")
        lines.append("")

    if data["rejected"]:
        lines.append("## Отклонено grounding-валидатором (анти-галлюцинации)\n")
        for item in data["rejected"]:
            f = item.get("finding", {})
            title = f.get("title", "(без названия)") if isinstance(f, dict) else str(f)
            lines.append(f"- `{item['file']}` строка {f.get('line') if isinstance(f, dict) else '?'}: "
                         f"{title} — причина: {item['reason']}")
        lines.append("")

    if data["errors"]:
        lines.append("## Ошибки обработки файлов\n")
        for e in data["errors"]:
            lines.append(f"- `{e['file']}`: {e['error']}")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="Локальный code review через LM Studio")
    ap.add_argument("repo", nargs="?", default=".", help="путь к папке с git-репозиторием")
    ap.add_argument("--base", help="сравнивать с веткой/ref (git diff <base>...HEAD)")
    ap.add_argument("--staged", action="store_true", help="только staged-изменения")
    ap.add_argument("--last", action="store_true", help="изменения последнего коммита")
    ap.add_argument("--model", help="id модели в LM Studio (по умолчанию автодетект)")
    ap.add_argument("--max-context-chars", type=int, default=24000,
                    help="бюджет контекста на один файл (символы)")
    ap.add_argument("--max-tokens", type=int, default=1800, help="максимум токенов ответа")
    ap.add_argument("--out", help="путь к файлу отчёта (по умолчанию рядом со скриптом)")
    ap.add_argument("--description", help="текстовое описание того, что сделано в изменениях")
    ap.add_argument("--description-file", help="файл с описанием изменений (UTF-8)")
    ap.add_argument("--no-llm", action="store_true", help="только git+контекст, без модели")
    ap.add_argument("--list", action="store_true", help="показать изменённые файлы и выйти")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        print(f"[Ошибка] Папка не найдена: {repo}")
        return 2

    if not git_ok(repo, "rev-parse", "--is-inside-work-tree"):
        print(f"[Ошибка] {repo} не является git-репозиторием.")
        return 2

    diff_args, base_ref, mode = resolve_review_target(args)
    try:
        targets = changed_files(repo, diff_args, base_ref,
                                include_untracked=not args.staged and not args.base and not args.last)
    except RuntimeError as e:
        print(f"[Ошибка git] {e}")
        return 2

    if not targets:
        print("[Система] Изменённых файлов не найдено.")
        return 0

    if args.list:
        for t in targets:
            print(f"{t['status']}\t{t['path']}")
        return 0

    model = args.model or detect_model()
    print(f"[Система] Репозиторий: {repo}")
    print(f"[Система] Режим: {mode}")
    print(f"[Система] Модель: {model}")
    print(f"[Система] Файлов к ревью: {len(targets)}")

    index = collect_index(repo)
    repo_map = build_repo_map(index)
    mech = mechanical_checks(repo, diff_args if diff_args != ["HEAD~1...HEAD"] else ["HEAD~1", "HEAD"])
    cache = {}

    description = ""
    if args.description_file:
        try:
            description = Path(args.description_file).read_text(encoding="utf-8")
        except OSError as e:
            print(f"[Предупреждение] Не удалось прочитать описание из файла: {e}")
    if args.description:
        description = args.description

    by_file = {}
    rejected = []
    errors = []
    severity_counts = {}
    tokens_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    started = time.time()

    for i, t in enumerate(targets, 1):
        print(f"\n[{i}/{len(targets)}] {t['path']} ...")
        related = find_related_files(repo, t["path"], index, cache)
        if args.no_llm:
            by_file[t["path"]] = []
            print(f"    diff={len(t['diff'])} симв., связанных файлов={len(related)}")
            continue
        findings, usage, err = review_file(t, repo_map, related, model,
                                           args.max_context_chars,
                                           description=description)
        for k in tokens_total:
            tokens_total[k] += int(usage.get(k, 0) or 0)
        if err:
            errors.append({"file": t["path"], "error": err})
            print(f"    [Ошибка] {err}")
            continue
        accepted, rej = ground_findings(findings, t["new_text"], t["old_text"], t["diff"])
        for r in rej:
            rejected.append({"file": t["path"], **r})
        by_file[t["path"]] = accepted
        for f in accepted:
            sev = f["severity"]
            severity_counts[sev] = severity_counts.get(sev, 0) + 1
        print(f"    замечаний: {len(accepted)} (отклонено {len(rej)})")

    duration = time.time() - started

    if args.out:
        out_path = Path(args.out)
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = SCRIPT_DIR / f"code_review_{stamp}.md"

    write_report(out_path, {
        "repo": repo, "mode": mode, "model": model,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "description": description,
        "duration": duration,
        "files_reviewed": len(by_file), "files_total": len(targets),
        "accepted_count": sum(len(v) for v in by_file.values()),
        "rejected_count": len(rejected),
        "tokens": {k: v for k, v in tokens_total.items() if v} or None,
        "mech": mech, "severity_counts": severity_counts,
        "by_file": by_file, "rejected": rejected, "errors": errors,
    })

    print(f"\n[Система] Готово за {duration:.1f} с.")
    print(f"[Система] Отчёт: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
