#!/usr/bin/env python3
"""
Локальный агент код-ревью на базе LM Studio (OpenAI-совместимый API).

Пайплайн:
  1. Находит изменённые файлы в git-репозитории.
     Режимы: uncommitted (staged+unstaged), --staged, --last, --base <ref>.
  2. Собирает контекст:
        - короткий заголовок: папка проекта и список изменённых файлов;
        - связанные файлы: импорты/require для TS/JS, namespace/using для C#,
          ссылки на объекты для SQL, плюс эвристический поиск по коду.
  3. Для каждого изменённого файла просит локальную LLM вернуть строгий JSON
     с замечаниями. Модель вызывается со стримингом (live-вывод) и reasoning
     (enable_thinking); reasoning сохраняется в отчёт.
  4. Механически валидирует ответ: JSON-схема + привязка строк и фрагментов
     к реальному коду (grounding). Непривязанные "галлюцинации" отбрасываются.
  5. Пишет Markdown-отчёт в подпапку reviews/ (добавлена в .gitignore).
     Флаг --batch проверяет все изменённые файлы одним промптом.

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
        _stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
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


PROMPT_FILE_PATH = None


def save_prompt(text):
    """Сохраняет отправляемый промпт в файл (для GUI/отладки)."""
    if PROMPT_FILE_PATH:
        try:
            Path(PROMPT_FILE_PATH).write_text(text, encoding="utf-8")
        except OSError:
            pass


def chat(messages, model, temperature=0.1, max_tokens=4096, timeout=600,
         think=True, on_delta=None):
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
    }
    if think:
        payload["chat_template_kwargs"] = {"enable_thinking": True}

    def _run():
        with requests.post(f"{LM_STUDIO_BASE}/chat/completions", json=payload,
                           timeout=timeout, stream=True) as resp:
            resp.raise_for_status()
            content, reasoning, usage = [], [], {}
            for raw in resp.iter_lines(decode_unicode=False):
                if not raw:
                    continue
                line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
                line = line.strip()
                if not line:
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                if line == "[DONE]":
                    break
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if chunk.get("usage"):
                    usage = chunk.get("usage") or {}
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                piece = delta.get("content") or ""
                rr = delta.get("reasoning_content") or ""
                if piece:
                    content.append(piece)
                    if on_delta:
                        on_delta("content", piece)
                if rr:
                    reasoning.append(rr)
                    if on_delta:
                        on_delta("reasoning", rr)
        return "".join(content), "".join(reasoning), usage

    try:
        return _run()
    except requests.exceptions.HTTPError as e:
        if think and e.response is not None and e.response.status_code in (400, 422):
            payload.pop("chat_template_kwargs", None)
            return _run()
        raise


def compile_project(repo: Path, targets) -> dict:
    changed_py = [t["path"] for t in targets if t["path"].endswith(".py")]
    slns = sorted(repo.glob("*.sln")) + sorted(repo.glob("*.slnx"))
    csprojs = sorted(repo.glob("*.csproj"))
    tsconfigs = sorted(repo.glob("tsconfig.json")) + sorted(repo.glob("*/*.tsconfig*.json"))[:1]

    if slns or csprojs:
        proj = str(slns[0]) if slns else str(csprojs[0])
        cmd = ["dotnet", "build", proj, "--nologo", "-v", "m"]
        kind = ".NET (dotnet build)"
    elif tsconfigs and any(t["path"].endswith((".ts", ".tsx", ".js", ".jsx")) for t in targets):
        cmd = ["npx", "--no-install", "tsc", "--noEmit", "-p", "."]
        kind = "TypeScript (tsc --noEmit)"
    elif changed_py:
        cmd = [sys.executable, "-m", "py_compile", *changed_py]
        kind = "Python (py_compile)"
    else:
        return {"kind": "нет статической компиляции (SQL/прочее)",
                "cmd": "", "ok": True, "output": "",
                "duration": 0.0}

    for exe in (cmd[0],):
        if exe in ("dotnet", "npx") and subprocess.run(
                ["where", exe], capture_output=True, shell=True).returncode != 0:
            return {"kind": kind, "cmd": " ".join(cmd), "ok": False,
                    "output": f"утилита '{exe}' не найдена в PATH", "duration": 0.0}

    started = time.time()
    try:
        proc = subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=300)
        output = (proc.stdout or "") + (proc.stderr or "")
        return {"kind": kind, "cmd": " ".join(cmd), "ok": proc.returncode == 0,
                "output": output.strip()[-16000:], "duration": time.time() - started}
    except subprocess.TimeoutExpired:
        return {"kind": kind, "cmd": " ".join(cmd), "ok": False,
                "output": "сборка превысила таймаут 300 c", "duration": time.time() - started}
    except Exception as e:
        return {"kind": kind, "cmd": " ".join(cmd), "ok": False,
                "output": str(e), "duration": time.time() - started}


def build_info_text(build) -> str:
    if not build or not build.get("cmd"):
        return ""
    if build.get("ok"):
        return f"OK ({build['kind']}, {build['duration']:.1f} c)"
    return (f"ПРОВАЛ ({build['kind']}, {build['duration']:.1f} c)\n"
            f"{build['output'][:4000] or '(без вывода)'}")


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


def build_project_header(repo: Path, targets) -> str:
    """Короткий заголовок промпта: папка проекта + изменённые файлы."""
    files = ", ".join(f"{t['path']} ({t['status']})" for t in targets)
    return f"ПРОЕКТ (папка): {repo}\nИЗМЕНЁННЫЕ ФАЙЛЫ: {files}"


def clip_section(text, limit, what):
    """Обрезает секцию промпта до лимита с заметкой в лог и в текст."""
    if len(text) <= limit:
        return text
    print(f"[Система] {what}: {len(text)} симв. — больше бюджета промпта, "
          f"усечено до {max(0, limit)} симв.", flush=True)
    marker = f"\n... [ПРЕДУПРЕЖДЕНИЕ: {what} обрезан; лимит {limit} симв.]"
    return text[:max(0, limit - len(marker))] + marker


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
            path = norm_path(path)
            if not path or not Path(path).suffix.lower() in CODE_EXT:
                continue
            if any(part in EXCLUDE_DIRS for part in path.split("/")):
                continue
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
    p = run_git(repo, "diff", "--check", *diff_args)
    if p.returncode == 0:
        return "git diff --check: OK (пробельные ошибки/маркеры конфликтов не найдены)"
    out = (p.stdout or "") + (p.stderr or "")
    return f"git diff --check: найдены проблемы:\n{out.strip()}"


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
        f["title"] = str(f.get("title") or f.get("message") or "").strip()
        f["comment"] = str(f.get("comment") or f.get("message") or "").strip()
        try:
            line = int(f.get("line", 0))
        except (TypeError, ValueError):
            line = 0
        evidence = str(f.get("evidence", "") or "").strip()
        ev_norm = normalize_ws(evidence)
        ev_ok = bool(evidence) and ev_norm in haystack

        line_ok = 1 <= line <= len(new_lines)
        if not line_ok and ev_ok:
            hit = next((i for i, ln in enumerate(new_lines, 1)
                        if ev_norm in normalize_ws(ln)), None)
            if hit:
                line = hit
                line_ok = True
        if not line_ok and not new_lines and old_text and 1 <= line <= len(old_lines):
            line_ok = True
        f["line"] = line

        if not line_ok:
            rejected.append({"finding": f,
                             "reason": f"строки {line} нет в файле"})
            continue
        if evidence and not ev_ok:
            rejected.append({"finding": f, "reason": "evidence не найден в коде/diff"})
            continue
        if not evidence:
            if line <= len(new_lines):
                f["evidence"] = new_lines[line - 1].strip()
            elif old_text:
                f["evidence"] = old_lines[line - 1].strip()
        f["grounded"] = True
        accepted.append(f)

    return accepted, rejected


def norm_path(p: str) -> str:
    p = (p or "").replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p


def ground_batch(findings, targets_by_path):
    lookup = {norm_path(k): v for k, v in targets_by_path.items()}
    accepted, rejected = [], []
    for f in findings:
        if not isinstance(f, dict):
            rejected.append({"file": "?", "finding": f, "reason": "не объект"})
            continue
        fpath = norm_path(str(f.get("file", "")))
        target = lookup.get(fpath)
        if not fpath or target is None:
            f["file"] = fpath or "?"
            rejected.append({"file": f["file"], "finding": f,
                             "reason": f"файл '{fpath}' не в списке изменённых"})
            continue
        f["file"] = next((k for k in targets_by_path if norm_path(k) == fpath), fpath)
        a, r = ground_findings([f], target["new_text"], target["old_text"], target["diff"])
        accepted.extend(a)
        for rr in r:
            rejected.append({"file": f["file"], **rr})
    return accepted, rejected


def review_batch(targets, project, model, max_chars, description="", retries=2,
                 think=True, max_tokens=4096, build_info=""):
    base_parts = [project]
    if description.strip():
        base_parts += ["", "ОПИСАНИЕ ИЗМЕНЕНИЙ (что планировалось сделать):", description.strip()]
    if build_info.strip():
        base_parts += ["", "СБОРКА ПРОЕКТА:", build_info.strip()]
    base = "\n".join(base_parts) + "\n\nИЗМЕНЁННЫЕ ФАЙЛЫ ДЛЯ РЕВЬЮ:"

    tail = (
        "\n\nЗАДАЧА: найди реальные проблемы в изменениях перечисленных файлов "
        "(баги, null/типизация, безопасность, async, производительность, "
        "SQL-инъекции и т.п.) с учётом контекста проекта и описания изменений.\n"
        "Каждое замечание обязано опираться на конкретную строку нового файла; "
        "в evidence вставь ТОЧНУЮ подстроку кода из нового файла или diff. "
        "Без подтверждения — не создавай замечание.\n"
        "Верни СТРОГО такой JSON: {\"findings\": [{\"file\": \"относительный путь\", "
        "\"severity\": \"critical|high|medium|low|nit\", \"category\": \"...\", "
        "\"line\": 42, \"title\": \"кратко\", \"comment\": \"...\", "
        "\"suggestion\": \"...\", \"evidence\": \"подстрока кода\"}]}\n"
        "Если проблем нет — верни {\"findings\": []}. Нумерация строк — по новому файлу."
    )

    avail = max_chars - len(base) - len(tail) - 512
    blocks = []
    for t in targets:
        lang = LANG_BY_EXT.get(Path(t["path"]).suffix.lower(), "?")
        diff = t.get("diff") or "(новый файл; см. полный текст ниже)"
        new_text = t.get("new_text") or "(файл удалён)"
        is_new = t["status"] == "A" or not (t.get("diff") or "").strip()
        ratio = 0.35 if is_new else 0.7
        db = max(max(int(avail * ratio), 0), 0)
        nb = max(avail - db, 0)
        diff = clip_section(diff, db, f"diff файла {t['path']}")
        new_text = clip_section(new_text, nb, f"полный текст файла {t['path']}")
        blocks.append(
            f"\n--- {t['path']} (статус {t['status']}, язык {lang}) ---\n"
            f"GIT DIFF:\n{diff}\n"
            f"ПОЛНЫЙ ТЕКСТ НОВОГО ФАЙЛА:\n{new_text}"
        )

    groups = []
    cur = ""
    for b in blocks:
        candidate = cur + b
        if cur and len(base) + len(candidate) + len(tail) > max_chars:
            groups.append(cur)
            cur = b
        else:
            cur = candidate
    if cur:
        groups.append(cur)

    all_findings = []
    all_reasoning = []
    usage_tot = {}
    errors = []
    for gi, g in enumerate(groups):
        prompt = base + g + tail
        if len(prompt) > max_chars:
            prompt = base + clip_section(g + tail, max_chars - len(base), "контент промпта")
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT + " В каждом замечании указывай поле file."},
            {"role": "user", "content": prompt},
        ]
        save_prompt(prompt)
        print(f"[Система] batch-промпт {gi + 1}/{len(groups)} ({len(prompt)} симв.)", flush=True)
        last_err = ""
        for attempt in range(retries + 1):
            reason_started = False

            def on_delta(kind, piece):
                nonlocal reason_started
                if kind == "reasoning":
                    if not reason_started:
                        print("\n[размышления агента] ", end="", flush=True)
                        reason_started = True
                    print(piece, end="", flush=True)
                else:
                    if reason_started:
                        print("\n[ответ агента] ", end="", flush=True)
                        reason_started = False
                    print(piece, end="", flush=True)

            try:
                content, reasoning, usage = chat(
                    messages, model, think=think, max_tokens=max_tokens, on_delta=on_delta)
                if not content.strip() and reasoning.strip():
                    print("\n[Система] reasoning съел бюджет токенов — повторяю без размышлений",
                          flush=True)
                    content, _, usage = chat(
                        messages, model, think=False, max_tokens=max_tokens, on_delta=None)
                print(flush=True)
                all_reasoning.append(reasoning)
                data = extract_json(content)
                findings = data.get("findings", [])
                if not isinstance(findings, list):
                    raise ValueError('поле "findings" не массив')
                all_findings += findings
                for k in usage_tot:
                    usage_tot[k] = usage_tot.get(k, 0) + int(usage.get(k, 0) or 0)
                break
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                messages.append({
                    "role": "user",
                    "content": f"Ответ не распарсился ({last_err}). Верни ТОЛЬКО валидный JSON.",
                })
        else:
            errors.append(f"промпт {gi + 1}: {last_err}")
    return all_findings, usage_tot, "; ".join(errors), "\n\n".join(all_reasoning)


def build_user_prompt(target, project, related, max_chars, description="", build_info=""):
    path = target["path"]
    lang = LANG_BY_EXT.get(Path(path).suffix.lower(), "?")
    fixed_parts = [
        project,
        "",
        f"ФАЙЛ: {path} (статус {target['status']}, язык {lang})",
    ]
    if description.strip():
        fixed_parts += ["", "ОПИСАНИЕ ИЗМЕНЕНИЙ (что планировалось сделать):", description.strip()]
    if build_info.strip():
        fixed_parts += ["", "СБОРКА ПРОЕКТА:", build_info.strip()]
    head = "\n".join(fixed_parts)

    tail = "\n".join([
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
    ])

    diff = target["diff"] or "(нет текстового diff, новый файл)"
    new_text = target["new_text"] if target["new_text"] else "(файл удалён)"
    is_new_file = target["status"] == "A" or not (target["diff"] or "").strip()

    avail = max_chars - len(head) - len(tail) - 256
    diff_budget = max(int(avail * (0.35 if is_new_file else 0.65)), 0)
    new_budget = max(avail - diff_budget, 0)
    diff = clip_section(diff, diff_budget, f"diff файла {path}")
    new_text = clip_section(new_text, new_budget, f"полный текст файла {path}")

    body = "\n\nGIT DIFF:\n" + diff + "\n\nПОЛНЫЙ ТЕКСТ НОВОГО ФАЙЛА:\n" + new_text

    related_budget = max_chars - len(head) - len(tail) - len(body)
    rel_parts = []
    if related and related_budget > 300:
        rel_lines = ["", "СВЯЗАННЫЕ ФАЙЛЫ (контекст, НЕ рецензируй их отдельно):"]
        for rel, body_txt in related:
            rel_lines.append(f"--- {rel} ---\n{body_txt}")
        rel_text = "\n".join(rel_lines)
        if len(rel_text) + len(head) + len(tail) + len(body) <= max_chars:
            rel_parts = [rel_text]
        else:
            rel_parts = [clip_section(rel_text, related_budget, "связанные файлы")]

    return head + body + "\n".join(rel_parts) + "\n" + tail


def review_file(target, project, related, model, max_chars, retries=2, description="",
                build_info="", think=True, max_tokens=4096):
    user = build_user_prompt(target, project, related, max_chars, description, build_info)
    save_prompt(user)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]
    last_err = ""
    for attempt in range(retries + 1):
        reason_started = False

        def on_delta(kind, piece):
            nonlocal reason_started
            if kind == "reasoning":
                if not reason_started:
                    print("\n[размышления агента] ", end="", flush=True)
                    reason_started = True
                print(piece, end="", flush=True)
            else:
                if reason_started:
                    print("\n[ответ агента] ", end="", flush=True)
                    reason_started = False
                print(piece, end="", flush=True)

        try:
            content, reasoning, usage = chat(
                messages, model, think=think, max_tokens=max_tokens, on_delta=on_delta)
            if not content.strip() and reasoning.strip():
                print("\n[Система] reasoning съел бюджет токенов — повторяю без размышлений",
                      flush=True)
                content, _, usage = chat(
                    messages, model, think=False, max_tokens=max_tokens, on_delta=None)
            print(flush=True)
            data = extract_json(content)
            findings = data.get("findings", [])
            if not isinstance(findings, list):
                raise ValueError('поле "findings" не массив')
            return findings, usage, "", reasoning
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            messages.append({
                "role": "user",
                "content": f"Ответ не распарсился ({last_err}). Верни ТОЛЬКО валидный JSON по схеме.",
            })
    return [], {}, last_err, ""


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

    if data.get("build") and data["build"].get("cmd"):
        build = data["build"]
        lines.append("## Сборка проекта\n")
        lines.append(f"- Тип: {build.get('kind', '')}")
        lines.append(f"- Команда: `{build['cmd']}`")
        lines.append(f"- Статус: {'OK' if build.get('ok') else 'ПРОВАЛ'} "
                     f"({build.get('duration', 0):.1f} c)")
        if build.get("output"):
            lines.append("\n<details>\n<summary>Вывод</summary>\n\n```")
            lines.append(build["output"][:8000])
            lines.append("```\n</details>\n")
        lines.append("")

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

    if data.get("reasoning"):
        lines.append("## Рассуждения агента (reasoning)\n")
        for rel, text in sorted(data["reasoning"].items()):
            text = (text or "").strip()
            if not text:
                continue
            if len(text) > 4000:
                text = text[:4000] + "\n... [усечено]"
            lines.append(f"### `{rel}`\n")
            lines.append("```")
            lines.append(text)
            lines.append("```\n")

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
    ap.add_argument("--max-tokens", type=int, default=4096,
                    help="максимум токенов ответа (учтите, что reasoning их тратит)")
    ap.add_argument("--out", help="путь к файлу отчёта (по умолчанию reviews/ рядом со скриптом)")
    ap.add_argument("--description", help="текстовое описание того, что сделано в изменениях")
    ap.add_argument("--description-file", help="файл с описанием изменений (UTF-8)")
    ap.add_argument("--batch", action="store_true",
                    help="проверять все изменённые файлы одним промптом")
    ap.add_argument("--no-compile", action="store_true", help="не компилировать проект")
    ap.add_argument("--no-thinking", action="store_true",
                    help="отключить reasoning (enable_thinking) в запросах к модели")
    ap.add_argument("--no-llm", action="store_true", help="только git+контекст, без модели")
    ap.add_argument("--list", action="store_true", help="показать изменённые файлы и выйти")
    ap.add_argument("--prompt-file", help="файл, в который записывается отправляемый промпт (UTF-8)")
    args = ap.parse_args()
    global PROMPT_FILE_PATH
    PROMPT_FILE_PATH = args.prompt_file

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
    project = build_project_header(repo, targets)
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

    build = None
    if not args.no_compile and not getattr(args, "list", False):
        print("[Система] Компилирую проект...")
        build = compile_project(repo, targets)
        status = "OK" if build.get("ok") else "ПРОВАЛ"
        print(f"[Сборка] {build.get('kind')} ({build.get('duration', 0):.1f} c) -> {status}")
        if not build.get("ok"):
            print(f"[Сборка] {build.get('output', '')[:1200]}")
    build_text = build_info_text(build)

    by_file = {}
    rejected = []
    errors = []
    severity_counts = {}
    reasoning_map = {}
    tokens_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    started = time.time()

    if args.batch and not args.no_llm:
        print("[Система] Режим batch: все изменённые файлы — одним промптом.")
        findings, batch_usage, err, reasoning = review_batch(
            targets, project, model, args.max_context_chars, description,
            think=not args.no_thinking, max_tokens=args.max_tokens, build_info=build_text)
        reasoning_map["(batch)"] = reasoning
        for k in tokens_total:
            tokens_total[k] += int(batch_usage.get(k, 0) or 0)
        if err:
            errors.append({"file": "(batch)", "error": err})
            print(f"    [Ошибка] {err}")
        accepted, rej = ground_batch(findings, {t["path"]: t for t in targets})
        for item in rej:
            rejected.append(item)
        for f in accepted:
            fpath = f.get("file", "?")
            by_file.setdefault(fpath, []).append(f)
            sev = f["severity"]
            severity_counts[sev] = severity_counts.get(sev, 0) + 1
        print(f"    замечаний: {len(accepted)} (отклонено {len(rej)})")
    else:
        for i, t in enumerate(targets, 1):
            print(f"\n[{i}/{len(targets)}] {t['path']} ...")
            related = find_related_files(repo, t["path"], index, cache)
            if args.no_llm:
                by_file[t["path"]] = []
                print(f"    diff={len(t['diff'])} симв., связанных файлов={len(related)}")
                continue
            findings, usage, err, reasoning = review_file(
                t, project, related, model, args.max_context_chars,
                description=description, build_info=build_text,
                think=not args.no_thinking, max_tokens=args.max_tokens)
            reasoning_map[t["path"]] = reasoning
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
        reviews_dir = SCRIPT_DIR / "reviews"
        reviews_dir.mkdir(parents=True, exist_ok=True)
        out_path = reviews_dir / f"code_review_{stamp}.md"

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
        "build": build, "reasoning": reasoning_map,
    })

    print(f"\n[Система] Готово за {duration:.1f} с.")
    print(f"[Система] Отчёт: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
