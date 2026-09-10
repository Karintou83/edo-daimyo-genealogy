"""
フェーズ2(データ整形): data/daimyo_raw.json から nodes/edges 形式の
data/tree.json を組み立てるスクリプト。

- nodes: 各人物について {id, name, han, generation, wikipedia_url, is_stub}
  (father_id はノードには含めない。エッジ構築にのみ使う)
- edges: father_id が存在する人物について {parent_id: father_id, child_id: id}

han が null のノード(長尾政景・吉良義央・上杉勝熙・前田利家)はそのまま
null として通し、無理に藩名を埋めない。
"""

import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
INPUT_PATH = BASE_DIR / "data" / "daimyo_raw.json"
OUTPUT_PATH = BASE_DIR / "data" / "tree.json"


def build_tree(raw: dict) -> dict:
    daimyo = raw["daimyo"]

    nodes = []
    for person in daimyo:
        nodes.append(
            {
                "id": person["id"],
                "name": person["name"],
                "han": person["han"],
                "generation": person["generation"],
                "wikipedia_url": person["wikipedia_url"],
                "is_stub": person["is_stub"],
            }
        )

    edges = []
    for person in daimyo:
        father_id = person.get("father_id")
        if father_id:
            edges.append({"parent_id": father_id, "child_id": person["id"]})

    return {"nodes": nodes, "edges": edges}


def summarize(tree: dict) -> None:
    nodes = tree["nodes"]
    edges = tree["edges"]

    node_count = len(nodes)
    edge_count = len(edges)

    stub_count = sum(1 for n in nodes if n["is_stub"])

    null_han_nodes = [n["name"] for n in nodes if n["han"] is None]

    parent_ids = {e["parent_id"] for e in edges}
    child_ids = {e["child_id"] for e in edges}
    connected_ids = parent_ids | child_ids
    isolated_names = [n["name"] for n in nodes if n["id"] not in connected_ids]

    print(f"ノード数: {node_count}")
    print(f"エッジ数: {edge_count}")
    print(f"is_stub: trueのノード数: {stub_count}")
    print(f"han: nullのノード数: {len(null_han_nodes)}")
    print(f"han: nullのノード氏名一覧: {', '.join(null_han_nodes) if null_han_nodes else '(なし)'}")
    print(f"孤立ノード数: {len(isolated_names)}")
    print(f"孤立ノード氏名一覧: {', '.join(isolated_names) if isolated_names else '(なし)'}")


def main() -> None:
    raw = json.loads(INPUT_PATH.read_text(encoding="utf-8"))
    tree = build_tree(raw)

    OUTPUT_PATH.write_text(
        json.dumps(tree, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    summarize(tree)


if __name__ == "__main__":
    main()
