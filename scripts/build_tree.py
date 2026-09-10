#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
フェーズ2(データ整形): data/raw/*.json (藩ごとの生データ) をすべて読み込んで統合し、
nodes/edges 形式の data/tree.json を組み立てるスクリプト。

設計方針は CLAUDE.md を参照。

- nodes: 各人物について {id, name, han, generation, wikipedia_url, image_url, is_stub}
  (father_id はノードには含めない。エッジ構築にのみ使う)
- edges: father_id が存在するレコードについて {parent_id: father_id, child_id: id}
- 同じQID(id)の人物が複数のrawファイルに重複して出てくることがある
  (ある藩の藩主一覧には載っていない父として、複数の藩からスタブノードとして
  参照されるケースなど)。QIDで重複排除し、内容が食い違う場合は警告を出す。
  重複排除の優先順位:
    1. is_stub=false のレコード(実際にその藩の藩主一覧に載っている)を、
       is_stub=true のレコード(他の藩からスタブとして参照されただけ)より優先する。
       採用前に、他ファイルのスタブレコードと氏名・Wikipediaリンク・画像・han
       (双方に値がある場合のみ)が食い違っていないか確認し、食い違いがあれば
       理由付きで警告を出す(採用結果自体はis_stub=false側で変わらない)。
    2. 同じ優先度のレコード同士で内容が食い違う場合は、最初に見つかったものを
       採用しつつ警告を出す(hanが空のレコードより埋まっているレコードを優先)。

han が null のノードはそのまま null として通し、無理に藩名を埋めない。

実行方法:
    python scripts/build_tree.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
RAW_DIR = BASE_DIR / "data" / "raw"
OUTPUT_PATH = BASE_DIR / "data" / "tree.json"

NODE_FIELDS = ["id", "name", "han", "generation", "wikipedia_url", "image_url", "is_stub"]
# 重複比較の対象にするフィールド(father_idはノードに含めないため比較対象外)
COMPARE_FIELDS = ["name", "han", "generation", "wikipedia_url", "image_url", "is_stub"]

WARNINGS: list[str] = []


def warn(message: str) -> None:
    WARNINGS.append(message)
    print(f"[WARN] {message}", file=sys.stderr)


def load_raw_files() -> list[dict]:
    """data/raw/*.json をすべて読み込む(ファイル名昇順で決定的な順序にする)。"""
    paths = sorted(RAW_DIR.glob("*.json"))
    if not paths:
        warn(f"{RAW_DIR} にJSONファイルが見つかりませんでした。先に scripts/fetch_daimyo_data.py を実行してください。")
    raws = []
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        raws.append(data)
    return raws


def merge_daimyo_records(raws: list[dict]) -> list[dict]:
    """
    全rawファイルのdaimyoレコードをQIDで重複排除する。
    戻り値は初出順を保った、id ごとに1件だけのレコードのリスト
    (father_idも含んだまま返す。エッジ構築に使うため)。
    """
    occurrences: dict[str, list[tuple[str, dict]]] = {}
    order: list[str] = []  # 初出順
    for raw in raws:
        slug = raw.get("slug", "?")
        for record in raw.get("daimyo", []):
            rid = record["id"]
            if rid not in occurrences:
                occurrences[rid] = []
                order.append(rid)
            occurrences[rid].append((slug, record))

    merged: list[dict] = []
    for rid in order:
        entries = occurrences[rid]
        if len(entries) == 1:
            merged.append(entries[0][1])
        else:
            merged.append(_resolve_duplicate(rid, entries))

    return merged


def _resolve_duplicate(rid: str, entries: list[tuple[str, dict]]) -> dict:
    """同一QIDの複数レコードから採用する1件を決め、必要なら警告を出す。"""
    non_stub = [(slug, r) for slug, r in entries if not r["is_stub"]]
    stub = [(slug, r) for slug, r in entries if r["is_stub"]]

    if len(non_stub) > 1:
        slugs = ", ".join(slug for slug, _ in non_stub)
        warn(
            f"id={rid}({entries[0][1]['name']}): 複数の藩([{slugs}])で is_stub=false の"
            f"レコードとして登場しています。最初に見つかったもの({non_stub[0][0]})を採用します。"
            f"対象藩の重複やテンプレートの内容を確認してください。"
        )
        return non_stub[0][1]

    if len(non_stub) == 1:
        chosen_slug, chosen = non_stub[0]
        # is_stub=falseのレコードを採用する前に、他ファイルのスタブレコードと
        # 身元に関わる項目(氏名・Wikipediaリンク・画像・han)が食い違っていないか確認する。
        # generation と is_stub はスタブ側が構造上異なるのが正常なので比較対象から除く。
        identity_fields = ["name", "wikipedia_url", "image_url"]
        for slug, r in stub:
            mismatches = []
            for f in identity_fields:
                if r.get(f) != chosen.get(f):
                    mismatches.append((f, r.get(f), chosen.get(f)))
            if r.get("han") is not None and chosen.get("han") is not None and r.get("han") != chosen.get("han"):
                mismatches.append(("han", r.get("han"), chosen.get("han")))
            if mismatches:
                detail = "; ".join(f"{f}: {sv!r}(stub) vs {cv!r}(採用)" for f, sv, cv in mismatches)
                warn(
                    f"id={rid}({chosen.get('name')}): {slug} 側のスタブレコードと "
                    f"{chosen_slug} 側の is_stub=false レコードで内容が食い違っています({detail})。"
                    f"is_stub=false のレコード({chosen_slug})を優先して採用します。"
                )
        return chosen

    # 全てstub: 内容が一致しているか確認し、一致しなければ警告。hanが埋まっている方を優先する。
    chosen_slug, chosen = stub[0]
    for slug, r in stub[1:]:
        for field in COMPARE_FIELDS:
            if r.get(field) != chosen.get(field):
                warn(
                    f"id={rid}({r.get('name')}): 複数の藩([{chosen_slug}, {slug}])のスタブノードで "
                    f"'{field}' の値が食い違っています"
                    f"({chosen.get(field)!r} vs {r.get(field)!r})。"
                    f"{chosen_slug} 側の値を採用します。"
                )
        if chosen.get("han") is None and r.get("han") is not None:
            chosen_slug, chosen = slug, r

    return chosen


def build_tree(merged_records: list[dict]) -> dict:
    nodes = [{field: r[field] for field in NODE_FIELDS} for r in merged_records]

    edges = []
    for r in merged_records:
        father_id = r.get("father_id")
        if father_id:
            edges.append({"parent_id": father_id, "child_id": r["id"]})

    return {"nodes": nodes, "edges": edges}


def summarize(tree: dict) -> None:
    nodes = tree["nodes"]
    edges = tree["edges"]

    node_count = len(nodes)
    edge_count = len(edges)
    stub_count = sum(1 for n in nodes if n["is_stub"])
    image_count = sum(1 for n in nodes if n.get("image_url"))
    null_han_nodes = [n["name"] for n in nodes if n["han"] is None]

    parent_ids = {e["parent_id"] for e in edges}
    child_ids = {e["child_id"] for e in edges}
    connected_ids = parent_ids | child_ids
    isolated_names = [n["name"] for n in nodes if n["id"] not in connected_ids]

    print(f"ノード数: {node_count}")
    print(f"エッジ数: {edge_count}")
    print(f"is_stub: trueのノード数: {stub_count}")
    print(f"image_urlありのノード数: {image_count}")
    print(f"han: nullのノード数: {len(null_han_nodes)}")
    print(f"han: nullのノード氏名一覧: {', '.join(null_han_nodes) if null_han_nodes else '(なし)'}")
    print(f"孤立ノード数: {len(isolated_names)}")
    print(f"孤立ノード氏名一覧: {', '.join(isolated_names) if isolated_names else '(なし)'}")
    if WARNINGS:
        print(f"警告 {len(WARNINGS)}件(標準エラー出力を参照)")


def main() -> None:
    raws = load_raw_files()
    merged_records = merge_daimyo_records(raws)
    tree = build_tree(merged_records)

    OUTPUT_PATH.write_text(
        json.dumps(tree, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    summarize(tree)


if __name__ == "__main__":
    main()
