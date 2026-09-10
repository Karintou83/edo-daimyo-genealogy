#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
フェーズ2(データ整形): data/raw/*.json (藩ごとの生データ) をすべて読み込んで統合し、
nodes/edges 形式の data/tree.json を組み立てるスクリプト。

設計方針は CLAUDE.md を参照。

- nodes: 各人物について {id, name, positions, wikipedia_url, image_url, is_stub}
  (father_id はノードには含めない。エッジ構築にのみ使う)。positions は
  {han, generation} の配列(1人が複数の藩/将軍職を務めた場合に対応する)。
- edges: father_id が存在するレコードについて {parent_id: father_id, child_id: id}
- 同じQID(id)の人物が複数のrawファイルに重複して出てくることがある
  (ある藩の藩主一覧には載っていない父として、複数の藩からスタブノードとして
  参照されるケースなど)。QIDで重複排除し、内容が食い違う場合は警告を出す。
  重複排除の優先順位:
    1. is_stub=false のレコード(実際にその藩の藩主一覧に載っている)を、
       is_stub=true のレコード(他の藩からスタブとして参照されただけ)より優先する。
       採用前に、他ファイルのスタブレコードと氏名・Wikipediaリンク・画像・positions内のhan
       (双方に値がある場合のみ)が食い違っていないか確認し、食い違いがあれば
       理由付きで警告を出す(採用結果自体はis_stub=false側で変わらない)。
    2. is_stub=false のレコード同士が複数のファイルにまたがって重複する場合
       (転封、または徳川吉宗のような兼務のケース)は、どちらか一方を選ばず
       positions 配列をマージする(重複するhan/generationの組は1つにまとめる)。
       name・wikipedia_url・image_url・father_id が食い違う場合は警告を出す。
    3. 同じ優先度のレコード同士(is_stub=true同士)で内容が食い違う場合は、
       最初に見つかったものを採用しつつ警告を出す(positionsが空のレコードより
       埋まっているレコードを優先)。

positions が空配列、または要素のhanがnullのノードはそのまま通し、無理に藩名を埋めない。

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

NODE_FIELDS = ["id", "name", "positions", "wikipedia_url", "image_url", "is_stub"]
# 重複比較の対象にするフィールド(father_idはノードに含めないため比較対象外)
COMPARE_FIELDS = ["name", "positions", "wikipedia_url", "image_url", "is_stub"]

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


def _merge_positions(position_lists: list[list[dict]]) -> list[dict]:
    """
    複数のpositions配列を(han, generation)の組で重複排除して結合し、year_start
    (取得できていればテンプレート由来、なければWikidataのP39由来)で時系列順に並べる。
    year_startが取れていない要素は末尾に回す。
    """
    merged: list[dict] = []
    seen: set[tuple[object, object]] = set()
    for positions in position_lists:
        for pos in positions:
            key = (pos.get("han"), pos.get("generation"))
            if key in seen:
                continue
            seen.add(key)
            merged.append(pos)
    merged.sort(key=lambda p: (p.get("year_start") is None, p.get("year_start")))
    return merged


def _resolve_duplicate(rid: str, entries: list[tuple[str, dict]]) -> dict:
    """同一QIDの複数レコードから採用する1件を決め、必要なら警告を出す。"""
    non_stub = [(slug, r) for slug, r in entries if not r["is_stub"]]
    stub = [(slug, r) for slug, r in entries if r["is_stub"]]

    if len(non_stub) > 1:
        # 転封・徳川吉宗のような兼務のケース。どちらか一方を選ばず、positions配列をマージする。
        slugs = ", ".join(slug for slug, _ in non_stub)
        chosen_slug, chosen = non_stub[0]
        merged_positions = _merge_positions([r.get("positions", []) for _, r in non_stub])

        # positions以外の身元に関わる項目(氏名・Wikipediaリンク・画像・father_id)が
        # 食い違っていないか確認する(食い違いがあってもマージ自体は続行し、警告のみ出す)。
        identity_fields = ["name", "wikipedia_url", "image_url", "father_id"]
        for slug, r in non_stub[1:]:
            mismatches = [
                (f, r.get(f), chosen.get(f)) for f in identity_fields if r.get(f) != chosen.get(f)
            ]
            if mismatches:
                detail = "; ".join(f"{f}: {sv!r}({slug}) vs {cv!r}({chosen_slug})" for f, sv, cv in mismatches)
                warn(
                    f"id={rid}({chosen.get('name')}): 複数の藩([{slugs}])で is_stub=false の"
                    f"レコードとして登場していますが、positions以外の項目が食い違っています({detail})。"
                    f"{chosen_slug} 側の値を採用しつつ、positionsのみマージします。"
                )

        merged = dict(chosen)
        merged["positions"] = merged_positions
        return merged

    if len(non_stub) == 1:
        chosen_slug, chosen = non_stub[0]
        # is_stub=falseのレコードを採用する前に、他ファイルのスタブレコードと
        # 身元に関わる項目(氏名・Wikipediaリンク・画像・positions内のhan)が食い違っていないか確認する。
        # is_stub はスタブ側が構造上異なるのが正常なので比較対象から除く。
        identity_fields = ["name", "wikipedia_url", "image_url"]
        for slug, r in stub:
            mismatches = []
            for f in identity_fields:
                if r.get(f) != chosen.get(f):
                    mismatches.append((f, r.get(f), chosen.get(f)))
            stub_hans = {p.get("han") for p in r.get("positions", []) if p.get("han")}
            chosen_hans = {p.get("han") for p in chosen.get("positions", []) if p.get("han")}
            if stub_hans and chosen_hans and not (stub_hans & chosen_hans):
                mismatches.append(("positions(han)", sorted(stub_hans), sorted(chosen_hans)))
            if mismatches:
                detail = "; ".join(f"{f}: {sv!r}(stub) vs {cv!r}(採用)" for f, sv, cv in mismatches)
                warn(
                    f"id={rid}({chosen.get('name')}): {slug} 側のスタブレコードと "
                    f"{chosen_slug} 側の is_stub=false レコードで内容が食い違っています({detail})。"
                    f"is_stub=false のレコード({chosen_slug})を優先して採用します。"
                )
        return chosen

    # 全てstub: 内容が一致しているか確認し、一致しなければ警告。positionsが埋まっている方を優先する。
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
        if not chosen.get("positions") and r.get("positions"):
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
    empty_positions_nodes = [n["name"] for n in nodes if not n.get("positions")]
    multi_positions_nodes = [n["name"] for n in nodes if len(n.get("positions", [])) > 1]
    total_positions = sum(len(n.get("positions", [])) for n in nodes)
    positions_with_years = sum(
        1 for n in nodes for p in n.get("positions", []) if p.get("year_start") is not None or p.get("year_end") is not None
    )

    parent_ids = {e["parent_id"] for e in edges}
    child_ids = {e["child_id"] for e in edges}
    connected_ids = parent_ids | child_ids
    isolated_names = [n["name"] for n in nodes if n["id"] not in connected_ids]

    print(f"ノード数: {node_count}")
    print(f"エッジ数: {edge_count}")
    print(f"is_stub: trueのノード数: {stub_count}")
    print(f"image_urlありのノード数: {image_count}")
    print(f"positions: 空配列のノード数: {len(empty_positions_nodes)}")
    print(f"positions: 空配列のノード氏名一覧: {', '.join(empty_positions_nodes) if empty_positions_nodes else '(なし)'}")
    print(f"positions: 複数要素(兼務・転封)のノード数: {len(multi_positions_nodes)}")
    print(f"positions: 複数要素のノード氏名一覧: {', '.join(multi_positions_nodes) if multi_positions_nodes else '(なし)'}")
    print(f"positions要素の総数: {total_positions}(うち在位年(year_start/year_endのいずれか)ありの要素数: {positions_with_years})")
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
