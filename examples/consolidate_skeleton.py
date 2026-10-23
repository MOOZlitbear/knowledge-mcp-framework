#!/usr/bin/env python3
"""自动整理骨架：扫描收件箱 → 生成归并方案 → 追加到项目卡 → 更新登记。

这是 docs/automation.md 的最小实现，刻意保留“只追加、可回滚”的约束。
依赖同目录的 kb_client.py；模型接口按 OpenAI 兼容的 /chat/completions 编写。

环境变量：
  LLM_BASE, LLM_KEY, LLM_MODEL   模型服务
  INBOX       收件箱目录（默认 inbox/）
  REG         处理登记（默认 inbox/registry.md）
  PROJECTS    项目卡目录（默认 projects/）
  APPLIED     已处理状态文件（默认 state/applied.json）
  AUTO_DIR    自动化产物目录（默认 90-automation/）
  MAX_ITEMS   单次上限（默认 20）

用法：
  python3 consolidate_skeleton.py --dry-run
  python3 consolidate_skeleton.py            # 提案 + 落卡
  python3 consolidate_skeleton.py --no-apply # 只写提案
"""
import json
import os
import re
import sys
import urllib.request
from datetime import datetime

from kb_client import Client

INBOX = os.environ.get("INBOX", "inbox/")
REG = os.environ.get("REG", INBOX + "registry.md")
PROJECTS = os.environ.get("PROJECTS", "projects/")
APPLIED = os.environ.get("APPLIED", "state/applied.json")
AUTO_DIR = os.environ.get("AUTO_DIR", "90-automation/")
MAX_ITEMS = int(os.environ.get("MAX_ITEMS", "20"))
LLM_BASE = os.environ.get("LLM_BASE", "")
LLM_KEY = os.environ.get("LLM_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "")
PII = [
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
    re.compile(r"病历号|身份证|手机号"),
]


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def pii(text):
    return any(p.search(text) for p in PII)


def tracked(kb):
    content = kb.call("read_note", {"path": REG})["content"]
    return set(re.findall(r"inbox/[A-Za-z0-9_\-./]+\.md", content))


def unprocessed(kb):
    paths = []
    offset = 0
    while True:
        batch = kb.call("list_notes", {"prefix": INBOX, "limit": 200,
                                       "offset": offset}).get("paths", [])
        paths += batch
        if len(batch) < 200:
            break
        offset += len(batch)
    done = tracked(kb)
    applied = load_json(APPLIED, {})
    return sorted(p for p in paths if p != REG and p.endswith(".md")
                  and p not in done and p not in applied)


def read_items(kb, paths):
    items, manual = [], []
    for p in paths[: MAX_ITEMS * 3]:
        d = kb.call("read_note", {"path": p})
        if pii(d["content"]):
            manual.append({"path": p, "reason": "疑似个人信息"})
            continue
        items.append({"path": p, "revision": d["revision"],
                      "title": d["content"].splitlines()[0].lstrip("# ").strip(),
                      "excerpt": re.sub(r"\s+", " ", d["content"])[:900]})
        if len(items) >= MAX_ITEMS:
            break
    return items, manual


def plan_with_llm(items, cards):
    schema = ('{"groups":[{"theme":"","destination":"projects/x.md","is_new":false,'
              '"items":["inbox/..."],"summary":"","append_markdown":"### ...",'
              '"new_card_markdown":""}],"unassigned":[]}')
    prompt = ("把下面条目按主题归并。已有卡能容纳就只给 append_markdown；"
              "确实需要新卡才 is_new=true 并给完整 new_card_markdown。"
              "每条必须且只能出现一次。不要输出个人信息或凭据。\n"
              f"项目卡：{json.dumps(cards, ensure_ascii=False)}\n"
              f"条目：{json.dumps(items, ensure_ascii=False)}\n{schema}")
    body = json.dumps({"model": LLM_MODEL, "temperature": 0.1,
                       "max_tokens": 8192,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(
        LLM_BASE.rstrip("/") + "/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + LLM_KEY}, method="POST")
    with urllib.request.urlopen(req, timeout=300) as r:
        text = json.loads(r.read())["choices"][0]["message"]["content"]
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    return json.loads(text)


def validate(plan, item_paths):
    groups = plan.get("groups", [])
    seen = set()
    for g in groups:
        dest = g.get("destination", "")
        if not dest.startswith(PROJECTS) or not dest.endswith(".md") or ".." in dest:
            raise ValueError(f"bad destination: {dest}")
        block = g.get("append_markdown") or g.get("new_card_markdown") or ""
        if not block.strip() or len(block) > 5000 or pii(block):
            raise ValueError("bad block")
        for p in g.get("items", []):
            if p not in item_paths or p in seen:
                raise ValueError(f"bad item: {p}")
            seen.add(p)
    return groups


def append_card(kb, g, date):
    dest = g["destination"]
    if g.get("is_new") and g.get("new_card_markdown"):
        try:
            kb.call("create_note", {"path": dest, "content": g["new_card_markdown"],
                                    "reason": f"auto merge {date}"})
            return
        except RuntimeError:
            pass
    old = kb.call("read_note", {"path": dest})
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    content = old["content"].rstrip() + f"\n\n## 自动整理 {stamp}\n\n" \
        + g["append_markdown"].strip() + "\n"
    kb.call("update_note", {"path": dest, "content": content,
                            "expected_revision": old["revision"],
                            "reason": f"auto merge {date}"})


def register(kb, date, rows):
    reg = kb.call("read_note", {"path": REG})
    sec = (f"## {date} 自动整理（{len(rows)} 条）\n\n"
           "| # | 路径 | 读到 revision | 落点 |\n|---|---|---|---|\n"
           + "\n".join(f"| {i} | {p} | {r} | `{d}` |"
                       for i, (p, r, d) in enumerate(rows, 1)) + "\n\n")
    anchor = reg["content"].find("## 说明")
    anchor = anchor if anchor >= 0 else len(reg["content"])
    content = reg["content"][:anchor] + sec + reg["content"][anchor:]
    kb.call("update_note", {"path": REG, "content": content,
                            "expected_revision": reg["revision"],
                            "reason": f"auto merge {date}"})


def main():
    dry = "--dry-run" in sys.argv
    apply = "--no-apply" not in sys.argv
    date = datetime.now().strftime("%Y-%m-%d")
    kb = Client()
    try:
        todo = unprocessed(kb)
        if not todo:
            print("no unprocessed items")
            return
        items, manual = read_items(kb, todo)
        cards = kb.call("list_notes", {"prefix": PROJECTS, "limit": 200}).get("paths", [])
        plan = plan_with_llm(items, cards)
        groups = validate(plan, {i["path"] for i in items})
        if dry:
            print(json.dumps(plan, ensure_ascii=False, indent=2)[:3000])
            return
        os.makedirs(AUTO_DIR + "proposals", exist_ok=True)
        applied = load_json(APPLIED, {})
        rows = []
        for g in groups:
            if apply:
                append_card(kb, g, date)
            for p in g["items"]:
                applied[p] = date
                rows.append((p, next(i["revision"] for i in items if i["path"] == p),
                             g["destination"]))
            save_json(APPLIED, applied)
        if apply:
            register(kb, date, rows)
        print(f"done: {len(rows)} items, {len(groups)} groups, manual={len(manual)}")
    finally:
        kb.close()


if __name__ == "__main__":
    main()
