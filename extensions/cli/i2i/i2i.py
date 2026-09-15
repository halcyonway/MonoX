"""i2i.py — I2I extension CLI: 模板 CRUD + 调 dashscope API 生成图。

通过 `exec_cli mono_i2i <subcmd> [args]` 调用，由 `extensions/cli/inner/server.py`
分发。也可直接 `python -m extensions.cli.i2i i2i <subcmd>` 跑（debug 用）。

CLI 目录约定：
  <skills_root>/../cli/i2i/                  # 本文件位置
    i2i.py
    templates/<name>.md                      # 模板

输出图片存到 `<workspace>/i2i/<ts>_<model>_<tag>.png`：
  <workspace> 默认 = .monox/workspace（可被 env I2I_WORKSPACE_DIR 覆盖）。
  templates 路径 = SKILL_DIR / "templates"（与本文件同目录）。

环境变量：
  DASHSCOPE_API_KEY  主（用户在 zshrc 也设了 ALI_YUN_API_KEY，但那是 Bailian app key，
                     不能直接调 model API，这里只当 fallback）
  ALI_YUN_API_KEY    fallback
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from extensions.cli.inner import common_util as cu

# ---- 路径常量 ----

# 注意：当作为 extension CLI 通过 server 调用时，本文件路径 = <repo>/extensions/cli/i2i/i2i.py
# 当作为 standalone skill 跑时也类似。templates 永远在 SKILL_DIR / "templates"。
SKILL_DIR = Path(__file__).resolve().parent            # extensions/cli/i2i/
TEMPLATES_DIR = SKILL_DIR / "templates"

# workspace 默认 = .monox/workspace/（MonoX 约定），可被 env I2I_WORKSPACE_DIR 覆盖
_DEFAULT_MONOX_ROOT = SKILL_DIR.parent.parent.parent   # extensions/cli/i2i → repo root
DEFAULT_WORKSPACE = Path(os.environ.get(
    "MONOX_WORKSPACE", _DEFAULT_MONOX_ROOT / ".monox" / "workspace"))
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


def _ensure_dirs() -> None:
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _template_path(name: str) -> Optional[Path]:
    """校验模板名，返回 Path；非法返回 None（caller 包 envelope error）。"""
    if not NAME_RE.match(name):
        return None
    return TEMPLATES_DIR / f"{name}.md"


def _parse_template(md_path: Path) -> tuple[dict, str]:
    """读模板文件，返回 (frontmatter dict, body)。"""
    text = md_path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}, text
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

def cmd_list(_args: argparse.Namespace) -> dict:
    """`list` → 列模板名 + description."""
    _ensure_dirs()
    files = sorted(TEMPLATES_DIR.glob("*.md"))
    templates = []
    for f in files:
        fm, _ = _parse_template(f)
        templates.append({
            "name": f.stem,
            "description": fm.get("description", "(no description)"),
        })
    return cu.ok({"templates": templates, "count": len(templates)})


def cmd_show(args: argparse.Namespace) -> dict:
    """`show <name>` → 返回模板全文 + frontmatter."""
    p = _template_path(args.name)
    if p is None:
        return cu.err(f"invalid template name {args.name!r}",
                      allowed_pattern=NAME_RE.pattern)
    _ensure_dirs()
    if not p.exists():
        return cu.err(f"template not found: {args.name}",
                      templates_dir=str(TEMPLATES_DIR))
    fm, body = _parse_template(p)
    return cu.ok({"name": args.name, "frontmatter": fm, "body": body})


def cmd_add(args: argparse.Namespace) -> dict:
    """`add <name> --description ...` 从 stdin 读 prompt 创建模板."""
    p = _template_path(args.name)
    if p is None:
        return cu.err(f"invalid template name {args.name!r}",
                      allowed_pattern=NAME_RE.pattern)
    _ensure_dirs()
    if p.exists() and not args.force:
        return cu.err(f"template {args.name!r} already exists",
                      hint="use --force to overwrite", path=str(p))
    # CLI server 模式下 stdin 不可用；提示用户走 file/pipe
    body = getattr(args, "_prompt_body", None)
    if body is None:
        return cu.err(
            "add requires prompt body",
            hint="(in CLI server mode, write templates directly to "
                 f"{TEMPLATES_DIR}/<name>.md or use force-rewrite)",
        )
    if not body.strip():
        return cu.err("empty prompt body")
    description = args.description or "(no description)"
    fm_lines = [f"description: {description}"]
    if args.tags:
        fm_lines.append(f"tags: [{', '.join(args.tags)}]")
    content = "---\n" + "\n".join(fm_lines) + "\n---\n\n" + body.strip() + "\n"
    p.write_text(content, encoding="utf-8")
    return cu.ok({"name": args.name, "path": str(p), "description": description})


def cmd_edit(args: argparse.Namespace) -> dict:
    """`edit <name>` → 用 $EDITOR 编辑（只在前台 shell 调用有效，server 模式返回 error）."""
    p = _template_path(args.name)
    if p is None:
        return cu.err(f"invalid template name {args.name!r}")
    _ensure_dirs()
    if not p.exists():
        return cu.err(f"template not found: {args.name}")
    editor = os.environ.get("EDITOR", "vi")
    # subprocess.call 是阻塞的，会让 server 卡住 — server 模式直接拒绝。
    return cu.err("edit not supported in CLI server mode",
                  hint=f"edit {p} manually with your editor of choice "
                       f"(or set EDITOR={editor})")


def cmd_rm(args: argparse.Namespace) -> dict:
    """`rm <name>` → 删模板；需要 --yes 跳过确认（CLI server 不能交互）。"""
    p = _template_path(args.name)
    if p is None:
        return cu.err(f"invalid template name {args.name!r}")
    if not p.exists():
        return cu.err(f"template not found: {args.name}")
    if not args.yes:
        return cu.err("rm requires --yes in CLI server mode",
                      hint="pass -y/--yes to confirm",
                      path=str(p))
    p.unlink()
    return cu.ok({"name": args.name, "removed": str(p)})


# ---- 核心：调 API ----

def _call_api(image_path: Path, prompt: str, model: str) -> dict:
    """调 dashscope multimodal endpoint，返回 envelope (ok 或 err)。
    成功时附 `image_url` 字段（24h 有效）。
    """
    api_key = _api_key()
    if not api_key:
        return cu.err(
            f"{_key_name()} not set",
            hint=f"export {_key_name()}=<your-key> before invoking mono_i2i",
        )
    if not image_path.exists():
        return cu.err(f"image not found: {image_path}")

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
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            resp_body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        return cu.err(f"dashscope HTTP {exc.code}",
                      upstream_code=exc.code, upstream_body=body_text[:500])
    except urllib.error.URLError as exc:
        return cu.err(f"dashscope unreachable: {exc.reason}")
    except TimeoutError:
        return cu.err("dashscope timeout", timeout_sec=180)

    try:
        data = json.loads(resp_body)
    except json.JSONDecodeError as exc:
        return cu.err(f"dashscope returned non-JSON: {exc}",
                      body_preview=resp_body[:300])

    if "output" not in data:
        return cu.err("no 'output' in response",
                      body_preview=json.dumps(data)[:300])
    choices = data["output"].get("choices", [])
    if not choices:
        return cu.err("empty choices",
                      body_preview=json.dumps(data)[:300])
    content = choices[0]["message"]["content"]
    image_url = next((c.get("image") for c in content if "image" in c), None)
    if not image_url:
        return cu.err("no image in content",
                      body_preview=json.dumps(content, ensure_ascii=False)[:300])
    return cu.ok({"image_url": image_url, "usage": data.get("usage", {}),
                  "key_used": _key_name()})


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

def cmd_apply(args: argparse.Namespace) -> dict:
    """`apply --image <path> --template <name>` → 用模板 prompt 调 API."""
    p = _template_path(args.template)
    if p is None:
        return cu.err(f"invalid template name {args.template!r}")
    _ensure_dirs()
    if not p.exists():
        return cu.err(f"template not found: {args.template}",
                      templates_dir=str(TEMPLATES_DIR))
    fm, prompt = _parse_template(p)
    call = _call_api(Path(args.image), prompt, args.model)
    if not call.get("ok"):
        return call
    image_url = call["data"]["image_url"]
    out = _output_path(args.model, args.template)
    try:
        n = _download(image_url, out)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return cu.err(f"download failed: {exc}",
                      image_url=image_url, model=args.model,
                      template=args.template, description=fm.get("description"))
    return cu.ok({
        "saved_path": str(out),
        "saved_bytes": n,
        "image_url": image_url,
        "model": args.model,
        "template": args.template,
        "template_description": fm.get("description"),
        "key_used": call["data"].get("key_used"),
        "usage": call["data"].get("usage", {}),
    })


def cmd_raw(args: argparse.Namespace) -> dict:
    """`raw --image <path> --prompt "..."` → 直接用 prompt 调 API，可选 --save-as 存模板."""
    _ensure_dirs()
    if not args.prompt.strip():
        return cu.err("--prompt is empty")
    call = _call_api(Path(args.image), args.prompt, args.model)
    if not call.get("ok"):
        return call
    image_url = call["data"]["image_url"]
    tag = args.save_as if args.save_as else "raw"
    out = _output_path(args.model, tag)
    try:
        n = _download(image_url, out)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return cu.err(f"download failed: {exc}", image_url=image_url)
    saved_template_path: Optional[str] = None
    if args.save_as:
        p = _template_path(args.save_as)
        if p is None:
            return cu.err(f"invalid --save-as name {args.save_as!r}")
        if p.exists() and not args.force:
            return cu.err(f"template {args.save_as!r} already exists",
                          hint="pass --force to overwrite", path=str(p))
        description = args.save_description or "saved from raw session"
        content = (
            f"---\ndescription: {description}\n---\n\n"
            f"{args.prompt.strip()}\n"
        )
        p.write_text(content, encoding="utf-8")
        saved_template_path = str(p)
    return cu.ok({
        "saved_path": str(out),
        "saved_bytes": n,
        "image_url": image_url,
        "model": args.model,
        "key_used": call["data"].get("key_used"),
        "usage": call["data"].get("usage", {}),
        "saved_template": saved_template_path,
    })


# ---- argparse + handler entry ----

def _build_parser() -> argparse.ArgumentParser:
    """Construct the argparse tree. Used by both `main()` (server/standalone)
    and the standalone __main__ entry."""
    ap = argparse.ArgumentParser(
        prog="mono_i2i",
        description="I2I extension CLI: template CRUD + dashscope I2I API",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="list templates")

    p_show = sub.add_parser("show", help="show template body")
    p_show.add_argument("name")

    p_add = sub.add_parser("add", help="add template (body from --prompt in server mode)")
    p_add.add_argument("name")
    p_add.add_argument("--description", default="")
    p_add.add_argument("--tags", nargs="*", default=[])
    p_add.add_argument("--prompt", default=None,
                       help="prompt body (CLI server mode requires this since stdin unavailable)")
    p_add.add_argument("--force", action="store_true")

    p_edit = sub.add_parser("edit", help="edit template (server mode not supported)")
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

    return ap


def main(args: list[str]) -> dict:
    """Registry handler entry point. Receives argv tail, returns envelope."""
    parser = _build_parser()
    try:
        parsed = parser.parse_args(args)
    except SystemExit:
        # argparse exits with code 2 on usage error; convert to envelope.
        return cu.err("invalid arguments",
                      hint="pass --help to see usage",
                      prog="mono_i2i")

    handlers = {
        "list": cmd_list,
        "show": cmd_show,
        "add": cmd_add,
        "edit": cmd_edit,
        "rm": cmd_rm,
        "apply": cmd_apply,
        "raw": cmd_raw,
    }
    handler = handlers[parsed.cmd]

    # 把 --prompt 注入 add 子命令的 _prompt_body 字段，避免依赖 stdin（server 模式无 stdin）
    if parsed.cmd == "add" and parsed.prompt is not None:
        object.__setattr__(parsed, "_prompt_body", parsed.prompt)

    return handler(parsed)


if __name__ == "__main__":
    """Standalone usage: `python -m extensions.cli.i2i.i2i <subcmd> [args]`."""
    sys.stdout.write(json.dumps(main(sys.argv[1:]), ensure_ascii=False, indent=2))
    sys.stdout.write("\n")
