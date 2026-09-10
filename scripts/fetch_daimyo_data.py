#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
フェーズ1: データ取得スクリプト

日本語版Wikipediaの藩主継承テンプレート(Navbox)から対象藩の藩主一覧と代数を取得し、
各人物の父をWikidata(P22)優先・Infobox(基礎情報 武士など)フォールバックで特定し、
肖像画像をWikidataのP18(image)から取得して、藩ごとに data/raw/<スラッグ>.json に保存する。

設計方針は CLAUDE.md を参照。
- つなぐ関係は実の血縁(親子)関係のみ。養子として藩主になった人物は実父とつなぐ。
- 人物の識別はWikidataのQIDを主キーとする(同姓同名の取り違えを避けるため)。QIDが
  取得できない稀なケースのみWikipedia記事名をIDとして代用する。
- 父の判定はWikidataのP22(父)プロパティを最優先とする。P22が取得できた場合、
  Infoboxから抽出した実父情報と比較し、一致していれば何もしない。一致しない、または
  Infobox側で父が特定できなかった場合は、P22の値を採用しつつ理由付きの警告を残す
  (サイレントに上書きしない)。P22が未登録の場合のみInfoboxの「父母」パラメータからの
  抽出結果をフォールバックとして使う(この場合もリンク先の記事からQIDを解決する)。
- 肖像画像はWikidataのP18(image)からCommonsのファイル名を取得し、
  https://commons.wikimedia.org/wiki/Special:FilePath/<ファイル名>?width=200 の形で
  image_url を組み立てる(P18が未登録なら None)。藩主・スタブノードの双方に持たせる。
- データは一度取得してJSONに保存する静的構成(サイト側はリアルタイムにWikipedia/Wikidata
  APIを叩かない)。
- 対象藩の藩主一覧に含まれない父はスタブノードとして追加し、そこから先は遡らない。
- 出力は藩ごとに独立したファイルに分ける。対象藩を増やしても既存の藩のファイルには
  影響を与えず、新しい藩のファイルだけが追加・更新される。

実行方法:
    python scripts/fetch_daimyo_data.py            # TARGET_HANS の全藩を取得
    python scripts/fetch_daimyo_data.py 米沢藩      # 藩名(またはスラッグ)を指定して一部だけ取得

出力:
    data/raw/<藩の英語表記スラッグ>.json  (例: data/raw/yonezawa.json, data/raw/kaga.json)
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

API_ENDPOINT = "https://ja.wikipedia.org/w/api.php"
WIKIDATA_ENDPOINT = "https://www.wikidata.org/w/api.php"
USER_AGENT = (
    "EdoDaimyoGenealogyBot/0.1 "
    "(https://github.com/Karintou83/edo-daimyo-genealogy; "
    "educational/research project; contact: tt0107kyr0274@gmail.com) "
    "Python-urllib"
)
REQUEST_INTERVAL_SEC = 1.0  # 連続リクエストの間隔(エンドポイントごとに個別に空ける)

# Wikidataの「父」プロパティ
P_FATHER = "P22"
# Wikidataの「画像」プロパティ
P_IMAGE = "P18"

# image_url に付けるサムネイル幅(px)
IMAGE_WIDTH = 200

# 対象藩: 藩名 -> {"template": 継承テンプレート名, "slug": 出力ファイル名に使う英語表記スラッグ}
# 藩を追加するときはここに1行足すだけでよい(既存の藩の出力ファイルには影響しない)。
TARGET_HANS: dict[str, dict[str, str]] = {
    "米沢藩": {"template": "Template:米沢藩主", "slug": "yonezawa"},
    "加賀藩": {"template": "Template:加賀藩主", "slug": "kaga"},
    "仙台藩": {"template": "Template:仙台藩主", "slug": "sendai"},
}

# 出力先ディレクトリ。藩ごとに <slug>.json を作る。
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"

# Infoboxとして扱うテンプレート名のプレフィックス(表記揺れに対応するため前方一致で判定)
# 「基礎情報 武士」系が中心だが、明治以降に華族・政治家として活動した人物(例: 最後の藩主)は
# 「政治家」「基礎情報 華族」テンプレートが使われることがある。これらには父母パラメータが
# 存在しないことが多く、その場合はInfobox自体は認識した上で「父の情報が見つからない」警告を出す。
INFOBOX_TEMPLATE_PREFIXES = [
    "基礎情報 武士",
    "基礎情報武士",
    "基礎情報 大名",
    "基礎情報大名",
    "基礎情報 公家",
    "基礎情報公家",
    "基礎情報 華族",
    "基礎情報華族",
    "政治家",
]

# 「父」を表すラベル(実父を優先する順)。値は正規表現の断片として使う。
FATHER_LABELS_PRIORITY = ["実父", "父"]
# 除外すべきラベル(実父ではないもの)
NON_FATHER_LABELS = ["養父", "義父", "母", "養母", "継父"]

# 警告は藩ごとのファイルに分けて出力するため、藩の処理開始時にリセットする。
WARNINGS: list[str] = []
TOTAL_WARNING_COUNT = 0


def reset_warnings() -> None:
    """藩ごとの処理を始める前に警告バッファを空にする。"""
    global WARNINGS
    WARNINGS = []


def warn(message: str) -> None:
    global TOTAL_WARNING_COUNT
    WARNINGS.append(message)
    TOTAL_WARNING_COUNT += 1
    print(f"[WARN] {message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# MediaWiki / Wikidata API アクセス
# ---------------------------------------------------------------------------

# エンドポイントごとに最終リクエスト時刻を管理し、それぞれ個別にウェイトを入れる
_last_request_time: dict[str, float] = {}


def api_get(endpoint: str, params: dict[str, str]) -> dict:
    """MediaWiki系API(Wikipedia/Wikidata共通)を呼び出し、JSONを返す。連続リクエストの間隔を空ける。"""
    global _last_request_time
    last = _last_request_time.get(endpoint, 0.0)
    elapsed = time.monotonic() - last
    if elapsed < REQUEST_INTERVAL_SEC:
        time.sleep(REQUEST_INTERVAL_SEC - elapsed)

    query = dict(params)
    query.setdefault("format", "json")
    url = f"{endpoint}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    finally:
        _last_request_time[endpoint] = time.monotonic()

    return data


def fetch_wikitext(title: str) -> Optional[str]:
    """指定タイトルの記事(またはテンプレート)のwikitextを取得する。存在しなければNone。"""
    data = api_get(API_ENDPOINT, {"action": "parse", "page": title, "prop": "wikitext", "redirects": "1"})
    if "error" in data:
        warn(f"『{title}』の取得に失敗しました: {data['error'].get('info', data['error'])}")
        return None
    try:
        return data["parse"]["wikitext"]["*"]
    except KeyError:
        warn(f"『{title}』のwikitextの形式が想定と異なります")
        return None


# --- Wikidata連携 -----------------------------------------------------------

_qid_cache: dict[str, Optional[str]] = {}
_claims_cache: dict[str, Optional[dict]] = {}
_entity_ja_info_cache: dict[str, tuple[Optional[str], Optional[str]]] = {}


def get_qid(title: str) -> Optional[str]:
    """Wikipedia記事タイトルからWikidataのQIDを取得する(pageprops経由)。キャッシュ付き。"""
    title = resolve_title(title)
    if title in _qid_cache:
        return _qid_cache[title]

    data = api_get(
        API_ENDPOINT,
        {"action": "query", "titles": title, "prop": "pageprops", "ppprop": "wikibase_item", "redirects": "1"},
    )
    qid: Optional[str] = None
    try:
        pages = data["query"]["pages"]
        for page in pages.values():
            if "missing" in page:
                continue
            qid = page.get("pageprops", {}).get("wikibase_item")
            break
    except KeyError:
        pass

    if qid is None:
        warn(f"『{title}』: WikidataのQIDを取得できませんでした。記事名をIDとして代用します。")

    _qid_cache[title] = qid
    return qid


def get_wikidata_claims(qid: str) -> Optional[dict]:
    """Wikidataエンティティのclaimsをwbgetentitiesで取得する。キャッシュ付き。"""
    if qid in _claims_cache:
        return _claims_cache[qid]

    data = api_get(WIKIDATA_ENDPOINT, {"action": "wbgetentities", "ids": qid, "props": "claims"})
    claims: Optional[dict] = None
    try:
        claims = data["entities"][qid]["claims"]
    except KeyError:
        warn(f"QID {qid}: Wikidataのclaims取得に失敗しました")

    _claims_cache[qid] = claims
    return claims


def get_father_qid_from_wikidata(qid: str) -> Optional[str]:
    """WikidataのP22(父)プロパティから父のQIDを取得する。未登録またはエラー時はNone。"""
    claims = get_wikidata_claims(qid)
    if not claims:
        return None
    statements = claims.get(P_FATHER)
    if not statements:
        return None
    for stmt in statements:
        mainsnak = stmt.get("mainsnak", {})
        if mainsnak.get("snaktype") != "value":
            continue
        value = mainsnak.get("datavalue", {}).get("value")
        if isinstance(value, dict) and value.get("entity-type") == "item":
            return value.get("id")
    return None


def build_commons_image_url(filename: str, width: int = IMAGE_WIDTH) -> str:
    """Commonsのファイル名から Special:FilePath 形式のサムネイルURLを組み立てる。"""
    encoded = urllib.parse.quote(filename.strip().replace(" ", "_"))
    return f"https://commons.wikimedia.org/wiki/Special:FilePath/{encoded}?width={width}"


def get_image_url_from_wikidata(node_id: str) -> Optional[str]:
    """
    WikidataのP18(image)からCommonsのファイル名を取得し、image_urlを組み立てる。
    P18が未登録の場合や、QIDが不明な場合はNoneを返す。
    claimsは父の判定(P22)と同じwbgetentitiesのレスポンスをキャッシュから使い回すため、
    追加のAPIリクエストは発生しない。
    """
    if not is_qid(node_id):
        return None
    claims = get_wikidata_claims(node_id)
    if not claims:
        return None
    statements = claims.get(P_IMAGE)
    if not statements:
        return None
    for stmt in statements:
        mainsnak = stmt.get("mainsnak", {})
        if mainsnak.get("snaktype") != "value":
            continue
        filename = mainsnak.get("datavalue", {}).get("value")
        if isinstance(filename, str) and filename.strip():
            return build_commons_image_url(filename)
    return None


def get_entity_ja_info(qid: str) -> tuple[Optional[str], Optional[str]]:
    """
    WikidataエンティティのQIDから、日本語ラベルと日本語版Wikipediaの記事タイトル(sitelink)を取得する。
    戻り値: (label_ja, jawiki_title)。いずれも見つからなければNone。キャッシュ付き。
    """
    if qid in _entity_ja_info_cache:
        return _entity_ja_info_cache[qid]

    data = api_get(
        WIKIDATA_ENDPOINT,
        {"action": "wbgetentities", "ids": qid, "props": "labels|sitelinks", "languages": "ja", "sitefilter": "jawiki"},
    )
    label: Optional[str] = None
    jawiki_title: Optional[str] = None
    try:
        entity = data["entities"][qid]
        label = entity.get("labels", {}).get("ja", {}).get("value")
        jawiki_title = entity.get("sitelinks", {}).get("jawiki", {}).get("title")
    except KeyError:
        pass

    result = (label, jawiki_title)
    _entity_ja_info_cache[qid] = result
    return result


def is_qid(value: str) -> bool:
    return bool(re.fullmatch(r"Q\d+", value))


def resolve_title(title: str) -> str:
    """記事タイトルを正規化(先頭のnamespace区切り等の簡単な整形のみ)。"""
    return title.strip()


def build_url(title: str) -> str:
    return "https://ja.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"))


# ---------------------------------------------------------------------------
# Navboxテンプレートから藩主一覧(代数=並び順)を抽出
# ---------------------------------------------------------------------------

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:\|[^\]]*)?\]\]")


def extract_ordered_daimyo_from_template(wikitext: str, han_name: str) -> list[str]:
    """
    Navboxのlist1= 以下から、各行(*で始まる箇条書き)の先頭のwikilinkを順番どおりに抽出する。
    リンクを含まない行(例: *廃藩置県)はスキップする。
    """
    # list1= から次の「|」区切り(次のNavboxパラメータ)または閉じ括弧までを取り出す
    m = re.search(r"\|\s*list1\s*=\s*(.*?)(?=\n\|\s*[A-Za-z0-9_]+\s*=|\n\}\}|\Z)", wikitext, re.S)
    if not m:
        warn(f"[{han_name}] テンプレートから list1= セクションを見つけられませんでした。手動確認が必要です。")
        return []

    list_block = m.group(1)
    names: list[str] = []
    for line in list_block.splitlines():
        line = line.strip()
        if not line.startswith("*"):
            continue
        link_match = WIKILINK_RE.search(line)
        if not link_match:
            # 例: 「*廃藩置県」のようなリンクを持たない行は代数に含めない
            if line.strip("* ").strip():
                warn(f"[{han_name}] リンクを含まない一覧項目をスキップしました: {line!r}")
            continue
        title = resolve_title(link_match.group(1))
        if title in names:
            warn(f"[{han_name}] 一覧内に重複したリンクがありました: {title!r}(2回目以降は無視)")
            continue
        names.append(title)

    if not names:
        warn(f"[{han_name}] 藩主を1人も抽出できませんでした。テンプレートの形式を確認してください。")

    return names


# ---------------------------------------------------------------------------
# Infoboxの抽出とパース
# ---------------------------------------------------------------------------

@dataclass
class InfoboxBlock:
    template_name: str
    params: dict[str, str]


def _find_balanced_template(wikitext: str, start_index: int) -> Optional[str]:
    """start_indexが指す '{{' から対応する '}}' までを、ネストを数えて取り出す。"""
    depth = 0
    i = start_index
    n = len(wikitext)
    while i < n - 1:
        two = wikitext[i:i + 2]
        if two == "{{":
            depth += 1
            i += 2
            continue
        if two == "}}":
            depth -= 1
            i += 2
            if depth == 0:
                return wikitext[start_index:i]
            continue
        i += 1
    return None


def extract_infobox(wikitext: str, article_title: str) -> Optional[InfoboxBlock]:
    """記事のwikitextから最初に見つかったInfoboxテンプレートを抽出してパースする。"""
    for prefix in INFOBOX_TEMPLATE_PREFIXES:
        pattern = re.compile(r"\{\{\s*" + re.escape(prefix))
        m = pattern.search(wikitext)
        if not m:
            continue
        block = _find_balanced_template(wikitext, m.start())
        if block is None:
            warn(f"『{article_title}』: Infoboxテンプレート({prefix})の括弧の対応が取れませんでした")
            continue
        params = _parse_template_params(block)
        return InfoboxBlock(template_name=prefix, params=params)

    warn(f"『{article_title}』: 既知のInfoboxテンプレートが見つかりませんでした(表記揺れの可能性。手動確認が必要です)")
    return None


def _parse_template_params(block: str) -> dict[str, str]:
    """
    '{{テンプレート名\n| param1 = value1\n| param2 = value2\n...}}' 形式を
    行頭の '| name =' を区切りとしてパースする。値が複数行にまたがる場合も対応する。
    """
    # 先頭の '{{テンプレート名' と末尾の '}}' を取り除く
    inner = block.strip()
    inner = re.sub(r"^\{\{[^\n|]*", "", inner, count=1)
    if inner.endswith("}}"):
        inner = inner[:-2]

    params: dict[str, str] = {}
    current_name: Optional[str] = None
    current_lines: list[str] = []

    param_header_re = re.compile(r"^\|\s*([^\s=]+)\s*=\s*(.*)$")

    for line in inner.split("\n"):
        header_match = param_header_re.match(line)
        if header_match:
            if current_name is not None:
                params[current_name] = "\n".join(current_lines).strip()
            current_name = header_match.group(1).strip()
            current_lines = [header_match.group(2)]
        else:
            if current_name is not None:
                current_lines.append(line)
            # current_nameがNoneの行(テンプレート名直後の空行等)は無視

    if current_name is not None:
        params[current_name] = "\n".join(current_lines).strip()

    return params


# ---------------------------------------------------------------------------
# 「父」の抽出
# ---------------------------------------------------------------------------

# 独立パラメータ名の候補(表記揺れ対応)
DIRECT_FATHER_PARAM_NAMES = ["実父", "父"]
# 「父・母」がまとまって入っているパラメータ名の候補
COMBINED_PARENT_PARAM_NAMES = ["父母", "両親"]


def _first_link_after_label(text: str, label_variants: list[str], exclude_labels: list[str]) -> Optional[str]:
    """
    text内で label_variants のいずれかのラベル(例: '父')の直後に現れる最初のwikilinkを返す。
    ただし exclude_labels (例: '養父') に該当する位置からのマッチは除外する。
    """
    # ラベルの位置をすべて洗い出し、除外ラベルと重なるものを取り除く。
    # ラベル自体がウィキリンク化されている場合(例: '[[父]]:...')にも対応するため、
    # ラベル文字列の直後に閉じ括弧']]'が0〜2個続くのを許容してからコロンを探す。
    LABEL_SUFFIX_RE = r"\]{0,2}\s*[:：]"
    label_positions: list[tuple[int, int, str]] = []  # (start, end, label)
    for label in label_variants:
        for lm in re.finditer(re.escape(label) + LABEL_SUFFIX_RE, text):
            label_positions.append((lm.start(), lm.end(), label))

    exclude_spans: list[tuple[int, int]] = []
    for ex_label in exclude_labels:
        for em in re.finditer(re.escape(ex_label) + LABEL_SUFFIX_RE, text):
            exclude_spans.append((em.start(), em.end()))

    def is_excluded(pos: int) -> bool:
        for es, ee in exclude_spans:
            # 除外ラベル(例:養父)のラベル文字列そのものと重なる場合のみ除外
            # 「養父」に対して「父」ラベルがマッチしてしまうケースをここで弾く
            if es <= pos < ee:
                return True
        return False

    # 開始位置が早い順に、除外に該当しないものを採用
    label_positions.sort(key=lambda t: t[0])
    for start, end, label in label_positions:
        if is_excluded(start):
            continue
        # ラベル以降、次のラベル(母/養父など)が出てくる前までを対象にリンクを探す
        rest = text[end:]
        next_label_m = re.search(
            "|".join(re.escape(l) + LABEL_SUFFIX_RE for l in (label_variants + exclude_labels) if l),
            rest,
        )
        segment = rest[: next_label_m.start()] if next_label_m else rest
        link_m = WIKILINK_RE.search(segment)
        if link_m:
            return resolve_title(link_m.group(1))
    return None


def extract_father(infobox: InfoboxBlock, article_title: str) -> Optional[str]:
    """
    Infoboxのパラメータから実父の記事タイトルを抽出する。
    優先順位:
      1. 独立パラメータ '実父' / '父'(値がそのままリンクの場合)
      2. 結合パラメータ '父母' / '両親' 内のラベル付きテキストから '実父' > '父' の順で抽出(養父等は除外)
    """
    params = infobox.params

    # 1. 独立パラメータとして存在するか
    for name in DIRECT_FATHER_PARAM_NAMES:
        if name in params and params[name].strip():
            value = params[name]
            link_m = WIKILINK_RE.search(value)
            if link_m:
                return resolve_title(link_m.group(1))
            else:
                warn(f"『{article_title}』: パラメータ「{name}」にwikilinkが見つかりません(値: {value[:50]!r})")

    # 2. 結合パラメータ(父母/両親)の中を探索
    for name in COMBINED_PARENT_PARAM_NAMES:
        if name in params and params[name].strip():
            value = params[name]
            father = _first_link_after_label(
                value,
                label_variants=FATHER_LABELS_PRIORITY,
                exclude_labels=NON_FATHER_LABELS,
            )
            if father:
                return father
            else:
                warn(
                    f"『{article_title}』: パラメータ「{name}」から実父のリンクを特定できませんでした"
                    f"(値: {value[:80]!r})。手動確認が必要です。"
                )

    warn(f"『{article_title}』: 父の情報を特定できませんでした(父母/両親/父/実父いずれのパラメータも空または未検出)")
    return None


def extract_han_hint(infobox: Optional[InfoboxBlock]) -> Optional[str]:
    """
    スタブノード用に、可能であればInfoboxの『藩』パラメータから藩名らしき文字列を抜き出す(ベストエフォート)。
    このパラメータは「[[日向国|日向]][[高鍋藩]]主」のように「国名リンク+藩名リンク」の順で
    書かれることが多いため、末尾が「藩」で終わるリンクを優先する(なければ最初のリンク、
    リンクが一つもなければプレーンテキストにフォールバック)。
    """
    if infobox is None:
        return None
    for name in ["藩"]:
        if name in infobox.params and infobox.params[name].strip():
            value = infobox.params[name]
            links = [resolve_title(m.group(1)) for m in WIKILINK_RE.finditer(value)]
            han_like = [t for t in links if t.endswith("藩")]
            if han_like:
                return han_like[0]
            if links:
                return links[0]
            # リンクがなければ生テキストからそれらしき部分を返す(簡易)
            plain = re.sub(r"\[\[|\]\]", "", value)
            plain = re.sub(r"<[^>]+>", "", plain).strip()
            return plain or None
    return None


# ---------------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------------

@dataclass
class DaimyoNode:
    id: str  # WikidataのQID。QIDが取得できない場合のみWikipedia記事名で代用する
    name: str
    han: Optional[str]
    generation: Optional[int]
    wikipedia_url: Optional[str]
    image_url: Optional[str]  # WikidataのP18(image)由来。画像がなければNone
    father_id: Optional[str]  # is_stubがTrueの場合は常にNone(さらに遡らないため)
    is_stub: bool


def determine_father(title: str, infobox: Optional[InfoboxBlock], node_id: str) -> Optional[str]:
    """
    人物(記事タイトル/ノードID)の父のID(QIDまたはフォールバックの記事名)を決定する。

    優先順位:
      1. Wikidata P22(node_idがQIDの場合)
      2. Infoboxの「父母」等から抽出した実父(P22が未登録の場合のみ)

    P22が取得できた場合は常にそれを採用する。Infobox側の実父情報と比較し、一致しない、
    またはInfobox側で特定できなかった場合はサイレントに上書きせず、理由付きの警告を残す。
    """
    infobox_father_title = extract_father(infobox, title) if infobox else None

    wikidata_father_qid: Optional[str] = None
    if is_qid(node_id):
        wikidata_father_qid = get_father_qid_from_wikidata(node_id)

    if wikidata_father_qid:
        if infobox_father_title:
            infobox_father_qid = get_qid(infobox_father_title)
            if infobox_father_qid != wikidata_father_qid:
                warn(
                    f"『{title}』: InfoboxとWikidataで父の情報が食い違っています"
                    f"(Infobox: {infobox_father_title} / QID {infobox_father_qid or '不明'}, "
                    f"Wikidata {P_FATHER}: {wikidata_father_qid})。Wikidataの値を採用します。"
                )
        else:
            warn(
                f"『{title}』: Infobox側では父を特定できませんでしたが、"
                f"Wikidataの{P_FATHER}から父({wikidata_father_qid})を解決しました。Wikidataの値を採用します。"
            )
        return wikidata_father_qid

    # P22が未登録(またはQID自体が不明)な場合のみInfoboxの抽出結果にフォールバックする
    if infobox_father_title:
        return get_qid(infobox_father_title) or infobox_father_title
    return None


def add_stub_node(father_id: str, all_nodes: dict[str, DaimyoNode]) -> None:
    """father_id(QIDまたは記事名フォールバック)からスタブノードの情報を解決して登録する。"""
    name: Optional[str] = None
    wikipedia_url: Optional[str] = None
    han_hint: Optional[str] = None
    article_title: Optional[str] = None

    if is_qid(father_id):
        label, jawiki_title = get_entity_ja_info(father_id)
        if jawiki_title:
            article_title = jawiki_title
            name = jawiki_title
        elif label:
            name = label
            warn(f"QID {father_id}: 対応する日本語版Wikipedia記事(sitelink)が見つからなかったため、Wikidataのラベルを名前として使用します(Wikipediaリンクなし)。")
        else:
            name = father_id
            warn(f"QID {father_id}: 名前(ラベル)も日本語版記事も取得できませんでした。QIDをそのまま名前として使用します。")
    else:
        # QIDが取得できなかった稀なケース: father_idは記事名そのもの
        article_title = father_id
        name = father_id

    if article_title:
        wikipedia_url = build_url(article_title)
        father_wikitext = fetch_wikitext(article_title)
        father_infobox = extract_infobox(father_wikitext, article_title) if father_wikitext else None
        han_hint = extract_han_hint(father_infobox)

    all_nodes[father_id] = DaimyoNode(
        id=father_id,
        name=name or father_id,
        han=han_hint,
        generation=None,
        wikipedia_url=wikipedia_url,
        image_url=get_image_url_from_wikidata(father_id),
        father_id=None,  # スタブノードはさらに遡らない
        is_stub=True,
    )


def process_han(han_name: str, template_title: str) -> dict[str, DaimyoNode]:
    """
    1つの藩を処理し、その藩の藩主ノードと(その藩の藩主一覧に載らない実父の)スタブノードを
    まとめた辞書を返す。藩ごとに独立した辞書を作るため、他の藩の処理結果には影響しない
    (同じ人物が複数の藩のファイルに現れることはあるが、その重複排除はフェーズ2で行う)。
    """
    print(f"=== {han_name} ({template_title}) ===")
    all_nodes: dict[str, DaimyoNode] = {}

    template_wikitext = fetch_wikitext(template_title)
    if template_wikitext is None:
        warn(f"[{han_name}] テンプレートを取得できなかったためスキップします")
        return all_nodes

    ordered_names = extract_ordered_daimyo_from_template(template_wikitext, han_name)
    print(f"  代数順の藩主一覧({len(ordered_names)}名): {', '.join(ordered_names)}")

    # 先にこの藩の一覧全員分のQIDを解決しておく(父がこの一覧に含まれるかどうかの判定に使うため)
    title_to_id: dict[str, str] = {}
    for name in ordered_names:
        qid = get_qid(name)
        title_to_id[name] = qid or name
    id_set = set(title_to_id.values())

    for idx, name in enumerate(ordered_names, start=1):
        node_id = title_to_id[name]
        if node_id in all_nodes and not all_nodes[node_id].is_stub:
            # 既に別の藩の一覧として処理済み(通常は起きない想定だが念のため)
            warn(f"『{name}』({node_id})は既に別の藩主として登録済みです。上書きせず警告のみ出します。")

        print(f"  [{idx}/{len(ordered_names)}] {name} ({node_id}) を取得中...")
        wikitext = fetch_wikitext(name)
        infobox = extract_infobox(wikitext, name) if wikitext else None
        if wikitext is None:
            warn(f"『{name}』の記事を取得できなかったため、Infoboxの解析はスキップします")

        father_id = determine_father(name, infobox, node_id)

        all_nodes[node_id] = DaimyoNode(
            id=node_id,
            name=name,
            han=han_name,
            generation=idx,
            wikipedia_url=build_url(name),
            image_url=get_image_url_from_wikidata(node_id),
            father_id=father_id,
            is_stub=False,
        )

        # 父が対象藩の藩主一覧に含まれない場合はスタブノードとして追加
        if father_id and father_id not in id_set and father_id not in all_nodes:
            print(f"    -> 父({father_id})は{han_name}主一覧に含まれないため、スタブノードとして追加します")
            add_stub_node(father_id, all_nodes)

    return all_nodes


def write_han_file(han_name: str, slug: str, template_title: str, nodes: dict[str, DaimyoNode]) -> Path:
    """1つの藩の取得結果を data/raw/<slug>.json に書き出す。"""
    output = {
        "generated_by": "scripts/fetch_daimyo_data.py",
        "han": han_name,
        "slug": slug,
        "template": template_title,
        "daimyo": [asdict(node) for node in nodes.values()],
        "warnings": list(WARNINGS),
    }

    output_path = OUTPUT_DIR / f"{slug}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    return output_path


def select_target_hans(args: list[str]) -> dict[str, dict[str, str]]:
    """
    コマンドライン引数で藩を絞り込む。引数がなければTARGET_HANSの全藩を対象にする。
    藩名(例: 米沢藩)でもスラッグ(例: yonezawa)でも指定できる。
    """
    if not args:
        return dict(TARGET_HANS)

    selected: dict[str, dict[str, str]] = {}
    for arg in args:
        for han_name, conf in TARGET_HANS.items():
            if arg == han_name or arg == conf["slug"]:
                selected[han_name] = conf
                break
        else:
            print(f"[ERROR] 対象藩に '{arg}' が見つかりません。"
                  f"指定できるのは: {', '.join(TARGET_HANS)} "
                  f"({', '.join(c['slug'] for c in TARGET_HANS.values())})", file=sys.stderr)
            sys.exit(1)
    return selected


def main() -> None:
    targets = select_target_hans(sys.argv[1:])

    written: list[tuple[str, Path, int]] = []
    for han_name, conf in targets.items():
        # 警告も藩ごとのファイルに分けて記録するため、藩の処理ごとにリセットする
        reset_warnings()
        nodes = process_han(han_name, conf["template"])
        output_path = write_han_file(han_name, conf["slug"], conf["template"], nodes)
        written.append((han_name, output_path, len(nodes)))
        with_image = sum(1 for n in nodes.values() if n.image_url)
        print(f"  -> {len(nodes)}件のノード(うち画像あり{with_image}件)を {output_path} に保存しました。")

    print("\n完了:")
    for han_name, output_path, count in written:
        print(f"  {han_name}: {count}件 -> {output_path}")
    if TOTAL_WARNING_COUNT:
        print(f"警告 {TOTAL_WARNING_COUNT}件が出力されました"
              f"(標準エラー出力および各藩のJSONの warnings を参照)。", file=sys.stderr)


if __name__ == "__main__":
    main()
