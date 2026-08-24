#!/usr/bin/env python3
"""i2i.py — I2I skill helper: 模板 CRUD + 调 API 生成图。

Skill 目录约定（由 SKILL.md 描述）：
  <skills_root>/i2i/
    SKILL.md                # 主入口（LLM 读这个）
    i2i.py                  # 本文件
    templates/<name>.md     # 模板（frontmatter + body 即 prompt）

输出图片存到 `<workspace>/i2i/<ts>_<model>_<tag>.png`：
  <workspace> 默认 = .monox/workspace（可被 --workspace-dir 覆盖）。
  <skills_root> 默认 = .monox/skills（可被 --skills-root 覆盖）。
  都从 SKILL 自身路径派生（i2i.py 父目录的父目录 = skills_root）。

用法（agent 调用）：
  python i2i.py list                                   # 列模板
  python i2i.py show <name>                            # 看模板内容
  python i2i.py add <name>                             # 从 stdin 读 prompt 写到 templates/<name>.md
  python i2i.py edit <name>                            # 用 $EDITOR 编辑
  python i2i.py rm <name>                              # 删
  python i2i.py apply --image <path> --template <name> [--model ...]
  python i2i.py raw --image <path> --prompt "..." [--model ...] [--save-as <name>]

环境变量：
  DASHSCOPE_API_KEY  主（用户在 zshrc 也设了 ALI_YUN_API_KEY，但那是 Bailian app key，
                     不能直接调 model API，这里只当 fallback）
  ALI_YUN_API_KEY    fallback
"""

import argparse
import base64
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

# ---- 路径常量 ----

SKILL_DIR = Path(__file__).resolve().parent            # .monox/skills/i2i/
SKILLS_ROOT = SKILL_DIR.parent                          # .monox/skills/
TEMPLATES_DIR = SKILL_DIR / "templates"

# workspace 默认 = .monox/workspace/（MonoX 约定），可被 env I2I_WORKSPACE_DIR 覆盖
DEFAULT_WORKSPACE = Path(os.environ.get("MONOX_WORKSPACE", SKILLS_ROOT.parent / "workspace"))
OUTPUT_DIR = Path(os.environ.get("I2I_WORKSPACE_DIR", DEFAULT_WORKSPACE / "i2i"))

ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
DEFAULT_MODEL = "qwen-image-3.0-pro"

# 模板文件名校验：ASCII 字母数字 + - _，避免路径注入
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


# ---- 工具 ----

def _api_key() -> Optional[str]:
    return os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("ALI_YUN_API_KEY")


def _key_name() -> str:
    return "DASHSCOPE_API_KEY" if os.environ.get("DASHSCOPE_API_KEY") else "ALI_YUN_API_KEY"


def _err(msg: str, code: int = 1) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def _ensure_dirs() -> None:
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _template_path(name: str) -> Path:
    if not NAME_RE.match(name):
        _err(f"invalid template name {name!r} (allowed: {NAME_RE.pattern})")
    return TEMPLATES_DIR / f"{name}.md"


def _parse_template(md_path: Path) -> tuple[dict, str]:
    """读模板文件，返回 (frontmatter dict, body)。frontmatter 必须以 --- 开头。

    格式：
      ---
      description: ...
      tags: [a, b]
      ---

      <prompt body>
    """
    text = md_path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}, text
    # 找第二个 ---
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    fm_block = text[3:end].strip()
    body = text[end + 4:].lstrip("\n")
    fm = {}
    for line in fm_block.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        fm[k.strip()] = v.strip()
    return fm, body


# ---- 模板 CRUD ----

def cmd_list(_args: argparse.Namespace) -> int:
    _ensure_dirs()
    files = sorted(TEMPLATES_DIR.glob("*.md"))
    if not files:
        print("(no templates yet)")
        return 0
    for f in files:
        fm, _ = _parse_template(f)
        desc = fm.get("description", "(no description)")
        print(f"{f.stem:30s}  {desc}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    _ensure_dirs()
    p = _template_path(args.name)
    if not p.exists():
        _err(f"template not found: {args.name}")
    sys.stdout.write(p.read_text(encoding="utf-8"))
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    _ensure_dirs()
    p = _template_path(args.name)
    if p.exists() and not args.force:
        _err(f"template {args.name!r} already exists; use --force to overwrite")
    # prompt 从 stdin 读（agent / shell heredoc 友好）
    if sys.stdin.isatty():
        _err("stdin is a TTY; pipe prompt via heredoc or stdin redirect")
    body = sys.stdin.read().strip()
    if not body:
        _err("empty prompt body")
    description = args.description or "(no description)"
    fm_lines = [f"description: {description}"]
    if args.tags:
        fm_lines.append(f"tags: [{', '.join(args.tags)}]")
    content = "---\n" + "\n".join(fm_lines) + "\n---\n\n" + body + "\n"
    p.write_text(content, encoding="utf-8")
    print(f"wrote {p}")
    return 0


def cmd_edit(args: argparse.Namespace) -> int:
    _ensure_dirs()
    p = _template_path(args.name)
    if not p.exists():
        _err(f"template not found: {args.name}")
    editor = os.environ.get("EDITOR", "vi")
    rc = subprocess.call([editor, str(p)])
    return rc


def cmd_rm(args: argparse.Namespace) -> int:
    p = _template_path(args.name)
    if not p.exists():
        _err(f"template not found: {args.name}")
    if not args.yes:
        ans = input(f"Delete {p}? [y/N] ").strip().lower()
        if ans != "y":
            print("aborted")
            return 1
    p.unlink()
    print(f"removed {p}")
    return 0


# ---- 核心：调 API ----

def _call_api(image_path: Path, prompt: str, model: str) -> str:
    """调 dashscope multimodal endpoint，返回生成图的 URL（24h 有效）。"""
    api_key = _api_key()
    if not api_key:
        _err("DASHSCOPE_API_KEY / ALI_YUN_API_KEY not set")
    if not image_path.exists():
        _err(f"image not found: {image_path}")

    mime = "image/jpeg" if image_path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    body = {
        "model": model,
        "input": {
            "messages": [{
                "role": "user",
                "content": [
                    {"image": f"data:{mime};base64,{b64}"},
                    {"text": prompt},
                ],
            }]
        },
        "parameters": {
            "n": 1,
            "watermark": False,
            "negative_prompt": " ",
            "prompt_extend": True,
        },
    }
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    print(f"POST {ENDPOINT} model={model} image={image_path}", file=sys.stderr)
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            resp_body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        _err(f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:300]}")
    data = json.loads(resp_body)
    if "output" not in data:
        _err(f"no 'output' in response: {json.dumps(data)[:300]}")
    choices = data["output"].get("choices", [])
    if not choices:
        _err(f"empty choices: {json.dumps(data)[:300]}")
    content = choices[0]["message"]["content"]
    image_url = next((c.get("image") for c in content if "image" in c), None)
    if not image_url:
        _err(f"no image in content: {json.dumps(content, ensure_ascii=False)[:300]}")
    usage = data.get("usage", {})
    if usage:
        print(f"usage: {json.dumps(usage)}", file=sys.stderr)
    return image_url


def _download(url: str, dst: Path, timeout: int = 60) -> int:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        data = r.read()
    dst.write_bytes(data)
    return len(data)


def _output_path(model: str, tag: Optional[str]) -> Path:
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_model = model.replace("/", "_").replace(":", "_")
    suffix = f"_{tag}" if tag else ""
    return OUTPUT_DIR / f"{ts}_{safe_model}{suffix}.png"


# ---- 子命令：apply / raw ----

def cmd_apply(args: argparse.Namespace) -> int:
    _ensure_dirs()
    p = _template_path(args.template)
    if not p.exists():
        _err(f"template not found: {args.template}")
    fm, prompt = _parse_template(p)
    print(f"template: {args.template}  description: {fm.get('description', '(none)')}", file=sys.stderr)
    url = _call_api(Path(args.image), prompt, args.model)
    out = _output_path(args.model, args.template)
    n = _download(url, out)
    print(f"saved {out} ({n} bytes)")
    print(f"url: {url}")
    return 0


def cmd_raw(args: argparse.Namespace) -> int:
    _ensure_dirs()
    if not args.prompt.strip():
        _err("--prompt is empty")
    url = _call_api(Path(args.image), args.prompt, args.model)
    tag = args.save_as if args.save_as else "raw"
    out = _output_path(args.model, tag)
    n = _download(url, out)
    print(f"saved {out} ({n} bytes)")
    print(f"url: {url}")
    if args.save_as:
        # 保存 prompt 到 templates/<save_as>.md（默认 force=False，已存在会问）
        p = _template_path(args.save_as)
        if p.exists() and not args.force:
            ans = input(f"template {args.save_as!r} exists; overwrite? [y/N] ").strip().lower()
            if ans != "y":
                print("(skipped save)")
                return 0
        description = args.save_description or f"saved from raw session"
        fm_lines = [f"description: {description}"]
        content = "---\n" + "\n".join(fm_lines) + "\n---\n\n" + args.prompt.strip() + "\n"
        p.write_text(content, encoding="utf-8")
        print(f"saved template {p}")
    return 0


# ---- argparse ----

def main() -> int:
    ap = argparse.ArgumentParser(description="I2I skill helper: template CRUD + dashscope I2I API")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="list templates")

    p_show = sub.add_parser("show", help="show template body")
    p_show.add_argument("name")

    p_add = sub.add_parser("add", help="add template from stdin")
    p_add.add_argument("name")
    p_add.add_argument("--description", default="")
    p_add.add_argument("--tags", nargs="*", default=[])
    p_add.add_argument("--force", action="store_true")

    p_edit = sub.add_parser("edit", help="edit template in $EDITOR")
    p_edit.add_argument("name")

    p_rm = sub.add_parser("rm", help="remove template")
    p_rm.add_argument("name")
    p_rm.add_argument("-y", "--yes", action="store_true")

    p_apply = sub.add_parser("apply", help="apply a template to an image")
    p_apply.add_argument("--image", required=True, help="input image path")
    p_apply.add_argument("--template", required=True)
    p_apply.add_argument("--model", default=DEFAULT_MODEL)

    p_raw = sub.add_parser("raw", help="ad-hoc prompt (no template)")
    p_raw.add_argument("--image", required=True)
    p_raw.add_argument("--prompt", required=True)
    p_raw.add_argument("--model", default=DEFAULT_MODEL)
    p_raw.add_argument("--save-as", help="save prompt as template after generation")
    p_raw.add_argument("--save-description", default="")
    p_raw.add_argument("--force", action="store_true")

    args = ap.parse_args()
    return {
        "list": cmd_list,
        "show": cmd_show,
        "add": cmd_add,
        "edit": cmd_edit,
        "rm": cmd_rm,
        "apply": cmd_apply,
        "raw": cmd_raw,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())