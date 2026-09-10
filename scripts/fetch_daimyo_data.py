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
- 各役職の在位年(year_start/year_end)は、テンプレート内の年数表記(例:「1590-1601」)を
  優先して抽出し、なければWikidataのP39(役職)の開始日・終了日(P580/P582)を
  フォールバックとして使う。どちらも取れなければ両方Noneにする。
- 肖像画像はWikidataのP18(image)からCommonsのファイル名を取得し、
  https://commons.wikimedia.org/wiki/Special:FilePath/<ファイル名>?width=200 の形で
  image_url を組み立てる(P18が未登録なら None)。藩主・スタブノードの双方に持たせる。
- データは一度取得してJSONに保存する静的構成(サイト側はリアルタイムにWikipedia/Wikidata
  APIを叩かない)。
- 対象藩の藩主一覧に含まれない父はスタブノードとして追加し、そこから先は遡らない。
- 出力は藩ごとに独立したファイルに分ける。対象藩を増やしても既存の藩のファイルには
  影響を与えず、新しい藩のファイルだけが追加・更新される。
- テンプレートから拾った候補リンクが実在の藩主かどうかは、藩主マスターリスト
  (Category:藩別の大名 配下の全「○○藩主」カテゴリ、加えてCategory:知藩事・
  Category:江戸幕府の征夷大将軍・Category:田安徳川家当主・Category:一橋徳川家当主・
  Category:清水徳川家当主から構築したID集合。初回実行時に構築し
  data/cache/daimyo_master_list.json にキャッシュして使い回す)に
  含まれているかで判定する。マスターリストにない候補(旧国名など。例: 佐倉藩テンプレート内の
  「常陸国」)は除外し、理由付きの警告を出す。詳細は build_daimyo_master_list() を参照。

実行方法:
    python scripts/fetch_daimyo_data.py                      # TARGET_HANS の全藩を取得
    python scripts/fetch_daimyo_data.py 米沢藩                # 藩名(またはスラッグ)を指定して一部だけ取得
    python scripts/fetch_daimyo_data.py --refresh-master-list # 藩主マスターリストを再取得してから実行

出力:
    data/raw/<藩の英語表記スラッグ>.json  (例: data/raw/yonezawa.json, data/raw/kaga.json)
    data/cache/daimyo_master_list.json    (藩主マスターリストのキャッシュ)
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
from datetime import datetime, timezone
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
# Wikidataの「役職」プロパティ(在位年のフォールバック取得に使う)
P_POSITION_HELD = "P39"
# 「役職」の修飾子: 開始日・終了日
P_START_TIME = "P580"
P_END_TIME = "P582"

# image_url に付けるサムネイル幅(px)
IMAGE_WIDTH = 200

# スタブノードの父をさらに遡って探索する最大世代数。
# 対象藩の藩主一覧に載らない人物(スタブ)が現れた場合、その人物自身を含めて最大この世代数まで
# 父を遡り、既知の藩主(対象藩の一覧、または既に取得済みの他藩の藩主一覧)、または
# 藩主マスターリスト(まだ取得していない藩の藩主も含む。build_daimyo_master_list参照)に
# 含まれる人物に行き着けば、そこで連鎖を止めて接続する(藩主マスターリストにしか
# 含まれない人物の場合は、その人物自身も新規のスタブノードとして追加したうえで打ち切る)。
# この世代数まで遡ってもどちらにも行き着かなければ、そこで遡るのをやめる
# (=それ以上は追わず、直接の父親1人だけを単独のスタブノードとして表示する)。
MAX_STUB_ANCESTOR_DEPTH = 5

# 対象藩: 藩名 -> {"template": 継承テンプレート名, "slug": 出力ファイル名に使う英語表記スラッグ}
# 藩を追加するときはここに1行足すだけでよい(既存の藩の出力ファイルには影響しない)。
TARGET_HANS: dict[str, dict[str, str]] = {
    "米沢藩": {"template": "Template:米沢藩主", "slug": "yonezawa"},
    "加賀藩": {"template": "Template:加賀藩主", "slug": "kaga"},
    "仙台藩": {"template": "Template:仙台藩主", "slug": "sendai"},
    "薩摩藩": {"template": "Template:薩摩藩主", "slug": "satsuma"},
    "長州藩": {"template": "Template:長州藩主", "slug": "choushuu"},
    "佐倉藩": {"template": "Template:佐倉藩主", "slug": "sakura"},
}

# 出力先ディレクトリ。藩ごとに <slug>.json を作る。
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"

# 藩主マスターリストの取得元カテゴリと、キャッシュファイルの保存先。
# build_daimyo_master_list() を参照。
#
# MASTER_LIST_SUBCAT_CATEGORIES: 「Category:○○藩主」のような藩ごとのサブカテゴリを
# 束ねる親カテゴリ。親カテゴリ自身の直属ページは対象外で、1階層下のサブカテゴリの
# 直属ページ(ns=0)を集める(理由はbuild_daimyo_master_list冒頭のコメントを参照)。
MASTER_LIST_SUBCAT_CATEGORIES = [
    "Category:藩別の大名",
]
# MASTER_LIST_DIRECT_CATEGORIES: そのカテゴリ自身の直属ページ(ns=0)をそのまま藩主
# マスターリストに追加する(下位カテゴリがあっても再帰しない)。
MASTER_LIST_DIRECT_CATEGORIES = [
    # 知藩事(版籍奉還〜廃藩置県の間、旧藩主がそのまま任じられた藩知事)。下位カテゴリなし。
    # 2026-09時点で304ページ。「廃藩置県」「版籍奉還」「府藩県三治制」など人物ではない
    # ページも直属ページとして混入しているが、マスターリストは候補の許可リストとして
    # 使うだけなので実害はない(該当ページがテンプレートの候補リンクとして出てくることは
    # 通常ない)。
    "Category:知藩事",
    # 江戸幕府の征夷大将軍(追贈された人物を含む)。下位カテゴリ4件
    # (将軍の御台所・側室・子女・征夷大将軍別のトピックス)は将軍本人ではないため
    # 再帰しない(実物をブラウザで確認済み)。
    "Category:江戸幕府の征夷大将軍",
    # 御三卿(田安家・一橋家・清水家)の当主。いずれも下位カテゴリなし(実物をブラウザで確認済み)。
    "Category:田安徳川家当主",
    "Category:一橋徳川家当主",
    "Category:清水徳川家当主",
]
MASTER_LIST_CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "cache" / "daimyo_master_list.json"

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


def _first_qualifier_year(qualifiers: dict, prop: str) -> Optional[int]:
    """statementのqualifiersから、指定プロパティ(P580/P582)の年(西暦)を取り出す。"""
    for q in qualifiers.get(prop, []):
        if q.get("snaktype") != "value":
            continue
        value = q.get("datavalue", {}).get("value")
        if isinstance(value, dict) and "time" in value:
            m = re.match(r"([+-]\d+)-\d\d-\d\dT", value["time"])
            if m:
                try:
                    return int(m.group(1))
                except ValueError:
                    return None
    return None


def get_wikidata_tenure_years(qid: str, context_label: Optional[str]) -> tuple[Optional[int], Optional[int]]:
    """
    WikidataのP39(役職)の開始日・終了日(P580/P582)から在位年をフォールバック取得する。
    1人が複数のP39を持つ場合(他の役職も含む)、role(値のQID)の日本語ラベルに
    context_label(藩名または藩名ヒント。例: '米沢藩')が含まれるものを優先する。
    見つからない、またはcontext_labelがない場合は、開始日/終了日を持つ最初の候補を使い、
    候補が複数あれば一意に決まらなかった旨を警告する。
    """
    claims = get_wikidata_claims(qid)
    if not claims:
        return None, None
    statements = claims.get(P_POSITION_HELD)
    if not statements:
        return None, None

    candidates: list[tuple[tuple[Optional[int], Optional[int]], Optional[str]]] = []
    for stmt in statements:
        mainsnak = stmt.get("mainsnak", {})
        if mainsnak.get("snaktype") != "value":
            continue
        value = mainsnak.get("datavalue", {}).get("value")
        position_qid = value.get("id") if isinstance(value, dict) and value.get("entity-type") == "item" else None
        qualifiers = stmt.get("qualifiers", {})
        year_start = _first_qualifier_year(qualifiers, P_START_TIME)
        year_end = _first_qualifier_year(qualifiers, P_END_TIME)
        if year_start is None and year_end is None:
            continue
        label = get_entity_ja_info(position_qid)[0] if position_qid else None
        candidates.append(((year_start, year_end), label))

    if not candidates:
        return None, None

    if context_label:
        for years, label in candidates:
            if label and context_label in label:
                return years

    if len(candidates) > 1:
        warn(
            f"QID {qid}: WikidataのP39(役職)に開始日/終了日を持つ候補が複数あり、"
            f"在位年の対応が一意に決まりませんでした。最初の候補を採用します。"
        )
    return candidates[0][0]


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

# 箇条書き行の中の年数レンジ表記(例: '1590-1601', '1590年 - 1601年', '1590〜1601')
YEAR_RANGE_RE = re.compile(r"(\d{3,4})\s*年?\s*[-–—〜~]\s*(\d{3,4})\s*年?")


@dataclass
class TemplateEntry:
    title: str
    year_start: Optional[int]
    year_end: Optional[int]


def _extract_year_range_from_line(line: str, link_span: tuple[int, int]) -> tuple[Optional[int], Optional[int]]:
    """
    箇条書き行から年数レンジ(例: '1590-1601')を抽出する。wikilink自体の文字列
    (記事名に数字を含む場合の誤検出を避けるため)は検索対象から除く。
    """
    search_text = line[:link_span[0]] + " " + line[link_span[1]:]
    m = YEAR_RANGE_RE.search(search_text)
    if not m:
        return None, None
    try:
        return int(m.group(1)), int(m.group(2))
    except ValueError:
        return None, None


# list1=, list2=, list3=, ... のように番号付きで複数のリストセクションに分かれている
# ことがある(藩主家が交代する藩に多い。例: 佐倉藩、古河藩)。番号の昇順で全セクションを
# 走査する。
LIST_SECTION_RE = re.compile(
    r"\|\s*(list(\d+))\s*=\s*(.*?)(?=\n\|\s*[A-Za-z0-9_]+\s*=|\n\}\}|\Z)", re.S
)


# ---------------------------------------------------------------------------
# 藩主マスターリスト(Category:藩別の大名 等から構築)
# ---------------------------------------------------------------------------
#
# 実装前の構造調査(2026-09時点、ブラウザで各カテゴリの実際のページを確認して判明した事実):
#   - Category:藩別の大名 自体は、下位カテゴリ(Category:○○藩主。328件)のみを持ち、
#     直属の記事ページは持たない。
#   - 各「○○藩主」カテゴリは、その藩の藩主全員(藩主家が交代した場合、交代前後の家も
#     すべて含む)を直属ページとして持つ。会津藩主(蒲生氏→加藤氏→保科氏→松平氏の全員)、
#     宇和島藩主、上田藩主など、家の交代がある藩を含む複数の藩で確認済み。
#   - 一部の著名な藩主は、自分の名前を冠したトピックカテゴリ(例: Category:保科正之)を
#     別途持つことがあるが、これは関連する寺社・郷土料理・古文書なども含む雑多な
#     トピック集であり藩主ではない項目を含む。かつ、その藩主自身は既に親の「○○藩主」
#     カテゴリに直属ページとして含まれているため、このサブカテゴリへ再帰しても取得漏れの
#     防止にはならず、ノイズが増えるだけ。**よって「○○藩主」カテゴリ配下のさらに下位の
#     カテゴリへは再帰しない(1階層のみ: 藩別の大名 → ○○藩主 → 直属ページ)**。
#   - 「○○藩主」カテゴリの直属ページには Template:○○藩主 自体が混ざることがあるため、
#     標準名前空間(ns=0)のみに絞って取得する。
#   - 藩主一覧テンプレートだけではカバーしきれない人物(知藩事、江戸幕府の征夷大将軍、
#     御三卿の当主など)を補うため、以下の5カテゴリも直属ページ(ns=0)のみをそのまま
#     マスターリストに追加する(いずれも実物を確認済み)。
#       - Category:知藩事: 下位カテゴリなし。304ページ(2026-09時点)。「廃藩置県」
#         「版籍奉還」等、人物でないページも直属ページとして混在しているが、マスターリストは
#         候補の許可リストとして使うだけなので実害はない。
#       - Category:江戸幕府の征夷大将軍: 下位カテゴリ4件(将軍の御台所・側室・子女・
#         征夷大将軍別のトピックス)は将軍本人ではないため再帰しない。追贈された人物
#         (徳川綱重など)も直属ページに含まれる。
#       - Category:田安徳川家当主・Category:一橋徳川家当主・Category:清水徳川家当主:
#         御三卿3家それぞれの当主一覧。いずれも下位カテゴリなし(田安8ページ・
#         一橋11ページ・清水7ページ。各カテゴリ内のTemplate:○○徳川家はns=0フィルタで除外)。


def fetch_subcategories(category_title: str) -> list[str]:
    """指定カテゴリの下位カテゴリ(Category:名前空間)のタイトル一覧を取得する(継続対応)。"""
    titles: list[str] = []
    params: dict[str, str] = {
        "action": "query",
        "list": "categorymembers",
        "cmtitle": category_title,
        "cmtype": "subcat",
        "cmlimit": "500",
    }
    while True:
        data = api_get(API_ENDPOINT, params)
        for member in data.get("query", {}).get("categorymembers", []):
            title = member.get("title")
            if title:
                titles.append(title)
        cont = data.get("continue", {}).get("cmcontinue")
        if not cont:
            break
        params["cmcontinue"] = cont
    return titles


def fetch_direct_page_qids(category_title: str) -> set[str]:
    """
    指定カテゴリ(例: Category:○○藩主)に直属する標準名前空間(ns=0)のページのIDを
    まとめて取得する(継続対応)。generatorを使い、1回(継続時は複数回)のAPI呼び出しで
    ページ一覧とWikidataのQID(pageprops経由)を同時に取得する。
    QIDが未登録のページは記事タイトルをフォールバックIDとして使う(get_qidが
    QID未登録時に記事名をIDとして使うのと同じ扱いに揃えるため)。
    取得したタイトル→QIDの対応は _qid_cache にも書き込み、以降の個別のget_qid呼び出しで
    同じ人物について重複してAPIを叩かずに済むようにする。
    """
    ids: set[str] = set()
    params: dict[str, str] = {
        "action": "query",
        "generator": "categorymembers",
        "gcmtitle": category_title,
        "gcmnamespace": "0",
        "gcmlimit": "500",
        "prop": "pageprops",
        "ppprop": "wikibase_item",
    }
    while True:
        data = api_get(API_ENDPOINT, params)
        pages = data.get("query", {}).get("pages", {})
        for page in pages.values():
            if "missing" in page:
                continue
            # gcmnamespace=0 をリクエストしているが、念のためレスポンス側でも標準名前空間
            # (ns=0)のページのみを採用する(Template:○○藩主 等の混入を防ぐ安全策)。
            if page.get("ns") != 0:
                continue
            title = resolve_title(page.get("title", ""))
            if not title:
                continue
            qid = page.get("pageprops", {}).get("wikibase_item")
            person_id = qid or title
            ids.add(person_id)
            _qid_cache[title] = qid
        cont = data.get("continue", {}).get("gcmcontinue")
        if not cont:
            break
        params["gcmcontinue"] = cont
    return ids


def build_daimyo_master_list(force_refresh: bool = False) -> set[str]:
    """
    藩主マスターリスト(実在の藩主・将軍・知藩事等として確認できる人物のID集合)を構築する。

    取得元は2種類ある(いずれもbuild_daimyo_master_list冒頭のコメントの調査結果に基づく)。
      - MASTER_LIST_SUBCAT_CATEGORIES: 「Category:○○藩主」のような藩ごとのサブカテゴリを
        束ねる親カテゴリ。下位カテゴリ(1階層のみ)を取得し、各下位カテゴリに直属する
        記事(ns=0)のIDを集める。
      - MASTER_LIST_DIRECT_CATEGORIES: そのカテゴリ自身に直属する記事(ns=0)を直接集める
        (下位カテゴリがあっても再帰しない)。知藩事・江戸幕府の征夷大将軍・清水徳川家当主
        など、藩主一覧テンプレートではカバーしきれない人物を補うために追加。

    一度構築した結果は MASTER_LIST_CACHE_PATH にキャッシュし、次回以降のスクリプト実行では
    再取得せずにそのまま使い回す(force_refresh=True、またはコマンドラインで
    --refresh-master-list を指定した場合のみ再取得する)。
    """
    if not force_refresh and MASTER_LIST_CACHE_PATH.exists():
        try:
            cached = json.loads(MASTER_LIST_CACHE_PATH.read_text(encoding="utf-8"))
            ids = set(cached["ids"])
            print(
                f"[藩主マスターリスト] キャッシュ({MASTER_LIST_CACHE_PATH})から{len(ids)}件のIDを"
                f"読み込みました(生成日時: {cached.get('generated_at', '不明')})。"
                f"再取得するには --refresh-master-list を指定してください。"
            )
            return ids
        except (json.JSONDecodeError, OSError, KeyError) as e:
            warn(f"[藩主マスターリスト] キャッシュの読み込みに失敗したため再取得します: {e!r}")

    all_ids: set[str] = set()
    subcategory_counts: dict[str, int] = {}

    for category in MASTER_LIST_SUBCAT_CATEGORIES:
        print(f"[藩主マスターリスト] {category} の下位カテゴリを取得中...")
        subcats = fetch_subcategories(category)
        subcategory_counts[category] = len(subcats)
        print(
            f"[藩主マスターリスト] {category}: 下位カテゴリ{len(subcats)}件を検出しました。"
            f"各カテゴリの直属ページを取得します(1件あたり約{REQUEST_INTERVAL_SEC:.0f}秒、"
            f"合計で数分かかります)..."
        )
        for i, subcat_title in enumerate(subcats, start=1):
            ids = fetch_direct_page_qids(subcat_title)
            all_ids |= ids
            if i % 20 == 0 or i == len(subcats):
                print(f"  [{category}] [{i}/{len(subcats)}] {subcat_title} まで処理済み(累計{len(all_ids)}件)")

    for category in MASTER_LIST_DIRECT_CATEGORIES:
        print(f"[藩主マスターリスト] {category} の直属ページを取得中...")
        ids = fetch_direct_page_qids(category)
        all_ids |= ids
        print(f"  [{category}] {len(ids)}件を追加しました(累計{len(all_ids)}件)")

    MASTER_LIST_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    cache_data = {
        "generated_by": "scripts/fetch_daimyo_data.py:build_daimyo_master_list",
        "source_subcat_categories": MASTER_LIST_SUBCAT_CATEGORIES,
        "source_direct_categories": MASTER_LIST_DIRECT_CATEGORIES,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "subcategory_counts": subcategory_counts,
        "id_count": len(all_ids),
        "ids": sorted(all_ids),
    }
    with MASTER_LIST_CACHE_PATH.open("w", encoding="utf-8") as f:
        json.dump(cache_data, f, ensure_ascii=False, indent=2)
    print(f"[藩主マスターリスト] {len(all_ids)}件のIDを {MASTER_LIST_CACHE_PATH} にキャッシュしました。")

    return all_ids


def is_candidate_in_master_list(title: str, master_ids: set[str]) -> bool:
    """
    テンプレートの一覧から拾った候補リンクが、藩主マスターリスト
    (build_daimyo_master_list参照。Category:藩別の大名 配下の全「○○藩主」カテゴリに加え、
    知藩事・江戸幕府の征夷大将軍・御三卿(田安・一橋・清水)の当主のカテゴリからも構築)に含まれているかを
    判定する。
    QIDが解決できればQIDで、解決できなければ記事タイトル(フォールバックID)で照合する
    (get_qidがQID未登録時に記事名をIDとして使うのと同じ扱いに揃えるため。
    build_daimyo_master_list側も同じ規則でフォールバックIDを登録している)。
    """
    qid = get_qid(title)
    candidate_id = qid or title
    return candidate_id in master_ids


def extract_ordered_daimyo_from_template(
    wikitext: str, han_name: str, master_ids: set[str]
) -> list[TemplateEntry]:
    """
    Navboxの list1=, list2=, list3=, ... を番号順にすべて読み込み、各行(*で始まる箇条書き)の
    先頭のwikilinkを順番どおりに抽出する(藩主家が交代する藩ではリストが複数に分かれることが
    あるため、存在するセクションを1つも取りこぼさず、代数はセクションをまたいで通し番号にする。
    藩主家が交代してもリセットしない)。リンクを含まない行(例: *廃藩置県)はスキップする。
    行内に年数レンジ表記(例: '1590-1601')があれば、在位年(year_start/year_end)として
    合わせて抽出する(見つからなければ両方None。その場合はWikidataのP39からのフォールバックで
    埋める)。
    候補として拾ったリンクは、藩主マスターリスト(master_ids。build_daimyo_master_list参照)に
    含まれているかを確認してから採用する。旧国名など藩主ではないリンク(例: 佐倉藩テンプレート内の
    「常陸国」)はマスターリストに含まれないため除外し、理由付きの警告を出す。
    """
    list_matches = sorted(LIST_SECTION_RE.finditer(wikitext), key=lambda m: int(m.group(2)))
    if not list_matches:
        warn(f"[{han_name}] テンプレートから list1=, list2=, ... のセクションを見つけられませんでした。手動確認が必要です。")
        return []

    if len(list_matches) > 1:
        section_names = "、".join(m.group(1) for m in list_matches)
        print(f"  [{han_name}] 複数のlistセクションを検出しました({section_names})。すべて順に読み込み、代数は通しで数えます。")

    entries: list[TemplateEntry] = []
    seen_titles: set[str] = set()
    for list_match in list_matches:
        section_name = list_match.group(1)
        list_block = list_match.group(3)
        for line in list_block.splitlines():
            line = line.strip()
            if not line.startswith("*"):
                continue
            link_match = WIKILINK_RE.search(line)
            if not link_match:
                # 例: 「*廃藩置県」のようなリンクを持たない行は代数に含めない
                if line.strip("* ").strip():
                    warn(f"[{han_name}] ({section_name}) リンクを含まない一覧項目をスキップしました: {line!r}")
                continue
            title = resolve_title(link_match.group(1))
            if title in seen_titles:
                warn(f"[{han_name}] ({section_name}) 一覧内に重複したリンクがありました: {title!r}(2回目以降は無視)")
                continue

            if not is_candidate_in_master_list(title, master_ids):
                warn(
                    f"[{han_name}] ({section_name}) リンク『{title}』は藩主マスターリスト"
                    f"({len(master_ids)}件)に含まれていないため、藩主候補から除外しました: {line!r}"
                )
                continue

            seen_titles.add(title)
            year_start, year_end = _extract_year_range_from_line(line, link_match.span())
            entries.append(TemplateEntry(title=title, year_start=year_start, year_end=year_end))

    if not entries:
        warn(f"[{han_name}] 藩主を1人も抽出できませんでした。テンプレートの形式を確認してください。")

    return entries


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
    # 役職の一覧。{"han": 藩名(または疑似藩名), "generation": 代数,
    # "year_start": 在位開始年, "year_end": 在位終了年}の配列。
    # 対象藩の藩主(is_stub=False)は現状1要素の配列になる(他の藩でも見つかった場合の
    # 統合はフェーズ2=build_tree.pyで行う)。スタブノードは、藩名ヒントが取得できれば
    # [{"han": ヒント, "generation": None, "year_start": ..., "year_end": ...}]、
    # 取得できなければ空配列[]にする。year_start/year_endはテンプレート内の年数表記を
    # 優先し、なければWikidataのP39(役職)の開始日・終了日をフォールバックにする
    # (どちらも取れなければNone)。
    positions: list[dict]
    wikipedia_url: Optional[str]
    image_url: Optional[str]  # WikidataのP18(image)由来。画像がなければNone
    father_id: Optional[str]  # is_stubがTrueの場合でも、既知の藩主に接続できればセットされる
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


@dataclass
class ResolvedPerson:
    """スタブノード候補1名分の解決済み情報(まだall_nodesには登録していない状態)。"""
    id: str
    name: str
    wikipedia_url: Optional[str]
    han_hint: Optional[str]
    image_url: Optional[str]
    article_title: Optional[str]
    infobox: Optional[InfoboxBlock]


def resolve_person_info(person_id: str) -> ResolvedPerson:
    """QID(または記事名フォールバック)から、名前・Wikipediaリンク・藩ヒント・画像・Infoboxを解決する。"""
    name: Optional[str] = None
    wikipedia_url: Optional[str] = None
    han_hint: Optional[str] = None
    article_title: Optional[str] = None
    infobox: Optional[InfoboxBlock] = None

    if is_qid(person_id):
        label, jawiki_title = get_entity_ja_info(person_id)
        if jawiki_title:
            article_title = jawiki_title
            name = jawiki_title
        elif label:
            name = label
            warn(f"QID {person_id}: 対応する日本語版Wikipedia記事(sitelink)が見つからなかったため、Wikidataのラベルを名前として使用します(Wikipediaリンクなし)。")
        else:
            name = person_id
            warn(f"QID {person_id}: 名前(ラベル)も日本語版記事も取得できませんでした。QIDをそのまま名前として使用します。")
    else:
        # QIDが取得できなかった稀なケース: person_idは記事名そのもの
        article_title = person_id
        name = person_id

    if article_title:
        wikipedia_url = build_url(article_title)
        wikitext = fetch_wikitext(article_title)
        infobox = extract_infobox(wikitext, article_title) if wikitext else None
        han_hint = extract_han_hint(infobox)

    return ResolvedPerson(
        id=person_id,
        name=name or person_id,
        wikipedia_url=wikipedia_url,
        han_hint=han_hint,
        image_url=get_image_url_from_wikidata(person_id),
        article_title=article_title,
        infobox=infobox,
    )


def find_ancestor_chain_to_known_daimyo(
    start_id: str,
    known_daimyo_ids: set[str],
    all_nodes: dict[str, DaimyoNode],
    master_ids: set[str],
    max_generations: int,
) -> tuple[list[ResolvedPerson], Optional[str], bool]:
    """
    start_id から父を最大 max_generations 世代分たどり、
      (a) known_daimyo_ids(対象藩の藩主一覧、または既に取得済みの他藩の藩主一覧)、
      (b) 既に all_nodes に登録済みの人物、
      (c) 藩主マスターリスト(master_ids。Category:藩別の大名 配下。まだ取得していない藩の
          藩主も含む全藩主のID集合)
    のいずれかに行き着く経路を探す。(a)(b)は「まだ取得していない藩の藩主」を含まないが、
    行き着いた人物は既にどこかにノードとして存在する(または今回の実行内で存在する)ため、
    参照するだけでよい。(c)のみに行き着いた場合は、その人物自身はまだどこにもノードとして
    存在しない(その藩をまだ取得していない)ため、呼び出し側でその人物自身もスタブノードとして
    新規に作る必要がある。

    戻り値は (chain, matched_id, matched_needs_new_stub)。
      - 見つかった場合: chain は start_id から(行き着いた人物の手前までの)世代の若い順の
        ResolvedPerson のリスト、matched_id は行き着いた藩主のID。
        matched_needs_new_stub は、行き着いた人物が(a)(b)ではなく(c)のみで見つかった場合に
        True(=その人物自身のノードをこれから作る必要がある)。
      - max_generations 世代たどっても見つからなかった場合: matched_id は None
        (matched_needs_new_stub は常にFalse)。
        chain には少なくとも start_id 自身(1人目)のResolvedPersonが入っている
        (呼び出し側が再度APIを叩かずに済むように、既に解決済みの情報として返す)。
    """
    chain: list[ResolvedPerson] = []
    current_id = start_id
    for _ in range(max_generations):
        person = resolve_person_info(current_id)
        chain.append(person)

        next_father_id = (
            determine_father(person.article_title, person.infobox, current_id)
            if person.article_title else None
        )
        if next_father_id:
            if next_father_id in known_daimyo_ids or next_father_id in all_nodes:
                return chain, next_father_id, False
            if next_father_id in master_ids:
                return chain, next_father_id, True
        if not next_father_id:
            break
        current_id = next_father_id

    return chain, None, False


def resolve_stub_tenure_years(person: "ResolvedPerson") -> tuple[Optional[int], Optional[int]]:
    """
    スタブノードの在位年をベストエフォートで解決する。スタブは対象藩のテンプレートに
    載っていない人物なので年数表記の取得元がなく、藩名ヒント(han_hint)が取れている場合のみ
    WikidataのP39(役職)からのフォールバックを試みる。取れなければ(None, None)。
    """
    if not person.han_hint or not is_qid(person.id):
        return None, None
    return get_wikidata_tenure_years(person.id, person.han_hint)


def add_stub_node(
    father_id: str,
    all_nodes: dict[str, DaimyoNode],
    known_daimyo_ids: set[str],
    master_ids: set[str],
    max_generations: int,
) -> None:
    """
    father_id(QIDまたは記事名フォールバック)からスタブノードを登録する。

    father_idの父をさらに最大max_generations世代分たどり、
      (a) 既知の藩主(known_daimyo_ids、または既にall_nodesに登録済みの人物)、
      (b) 藩主マスターリスト(master_ids)に含まれる人物(まだ取得していない藩の藩主も含む)
    のいずれかに行き着く経路が見つかった場合、father_idからその手前までの人物すべてを
    スタブノードの連鎖として追加する。
      - (a)に行き着いた場合: その人物は既にどこかにノードとして存在するので、参照するだけで
        新規ノードは作らない(最後の人物のfather_idを行き着いた藩主のIDに設定する)。
      - (b)のみで行き着いた場合: その人物はまだどこにもノードとして存在しない(その藩を
        まだ取得していない)ため、その人物自身も(父を持たない終端の)スタブノードとして
        新規に追加した上で、そこで探索を打ち切る(藩主マスターリストに載っている=実在の
        藩主だと確認できた時点で、それ以上遡らない)。
    いずれにも行き着かなかった場合は、中間の世代は一切追加せず、father_id自身だけを
    (father_id=Noneの)単独のスタブノードとして追加する(=父親だけを表示する)。
    """
    if father_id in all_nodes:
        return

    chain, matched_id, matched_needs_new_stub = find_ancestor_chain_to_known_daimyo(
        father_id, known_daimyo_ids, all_nodes, master_ids, max_generations
    )

    if matched_id and not matched_needs_new_stub:
        print(
            f"    -> {chain[0].name}の祖先が既知の藩主({matched_id})まで{len(chain)}世代でつながったため、"
            f"経路上の{len(chain)}名をスタブノードとして追加します"
        )
        for i, person in enumerate(chain):
            next_id = chain[i + 1].id if i + 1 < len(chain) else matched_id
            year_start, year_end = resolve_stub_tenure_years(person)
            all_nodes[person.id] = DaimyoNode(
                id=person.id,
                name=person.name,
                positions=(
                    [{"han": person.han_hint, "generation": None, "year_start": year_start, "year_end": year_end}]
                    if person.han_hint
                    else []
                ),
                wikipedia_url=person.wikipedia_url,
                image_url=person.image_url,
                father_id=next_id,
                is_stub=True,
            )
        return

    if matched_id and matched_needs_new_stub:
        # 藩主マスターリストには含まれるが、まだ取得していない藩の藩主なのでノードが
        # 存在しない。そこまでの中間世代をスタブとして連鎖させ、行き着いた人物自身も
        # (父を持たない)終端のスタブノードとして追加し、そこで探索を打ち切る。
        matched_person = resolve_person_info(matched_id)
        full_chain = chain + [matched_person]
        print(
            f"    -> {chain[0].name}の祖先が藩主マスターリストに含まれる{matched_person.name}"
            f"({matched_id}、未取得の藩の藩主)まで{len(chain)}世代でつながったため、"
            f"経路上の{len(full_chain)}名をスタブノードとして追加し、そこで探索を打ち切ります"
        )
        for i, person in enumerate(full_chain):
            next_id = full_chain[i + 1].id if i + 1 < len(full_chain) else None
            year_start, year_end = resolve_stub_tenure_years(person)
            all_nodes[person.id] = DaimyoNode(
                id=person.id,
                name=person.name,
                positions=(
                    [{"han": person.han_hint, "generation": None, "year_start": year_start, "year_end": year_end}]
                    if person.han_hint
                    else []
                ),
                wikipedia_url=person.wikipedia_url,
                image_url=person.image_url,
                father_id=next_id,
                is_stub=True,
            )
        return

    # max_generations世代たどっても(既知の藩主にも藩主マスターリストにも)行き着かなかった。
    # 中間の世代は表示せず、父親(father_id)だけを単独のスタブノードとして追加する。
    person = chain[0]  # 既に解決済みなので再取得はしない
    warn(
        f"『{person.name}』: 父をさらに{max_generations}世代遡っても藩主に行き着かなかったため、"
        f"この人物だけをスタブノードとして表示します(途中の世代は表示しません)。"
    )
    stub_year_start, stub_year_end = resolve_stub_tenure_years(person)
    all_nodes[father_id] = DaimyoNode(
        id=person.id,
        name=person.name,
        positions=(
            [{"han": person.han_hint, "generation": None, "year_start": stub_year_start, "year_end": stub_year_end}]
            if person.han_hint
            else []
        ),
        wikipedia_url=person.wikipedia_url,
        image_url=person.image_url,
        father_id=None,
        is_stub=True,
    )


def load_known_daimyo_ids_from_raw_files() -> set[str]:
    """
    data/raw/*.json (既に取得済みの他藩のファイル)から、is_stub=falseの人物のQID一覧を集める。
    スタブノードの父をさらに遡る際、対象藩以外の既知の藩主に行き着いた場合にも連鎖を
    止められるようにするために使う。ファイルが存在しない/壊れている場合は無視する。
    """
    known_ids: set[str] = set()
    if not OUTPUT_DIR.exists():
        return known_ids
    for path in OUTPUT_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for record in data.get("daimyo", []):
            if not record.get("is_stub", True):
                known_ids.add(record["id"])
    return known_ids


def process_han(han_name: str, template_title: str, master_ids: set[str]) -> dict[str, DaimyoNode]:
    """
    1つの藩を処理し、その藩の藩主ノードと(その藩の藩主一覧に載らない実父の)スタブノードを
    まとめた辞書を返す。藩ごとに独立した辞書を作るため、他の藩の処理結果には影響しない
    (同じ人物が複数の藩のファイルに現れることはあるが、その重複排除はフェーズ2で行う)。
    master_idsは藩主マスターリスト(build_daimyo_master_list参照)。テンプレートから拾った
    候補が実在の藩主かどうかの判定に使う。
    """
    print(f"=== {han_name} ({template_title}) ===")
    all_nodes: dict[str, DaimyoNode] = {}

    template_wikitext = fetch_wikitext(template_title)
    if template_wikitext is None:
        warn(f"[{han_name}] テンプレートを取得できなかったためスキップします")
        return all_nodes

    ordered_entries = extract_ordered_daimyo_from_template(template_wikitext, han_name, master_ids)
    ordered_names = [e.title for e in ordered_entries]
    print(f"  代数順の藩主一覧({len(ordered_names)}名): {', '.join(ordered_names)}")

    # 先にこの藩の一覧全員分のQIDを解決しておく(父がこの一覧に含まれるかどうかの判定に使うため)
    title_to_id: dict[str, str] = {}
    for name in ordered_names:
        qid = get_qid(name)
        title_to_id[name] = qid or name
    id_set = set(title_to_id.values())
    # 対象藩の一覧に加え、既に取得済みの他藩の藩主一覧も「既知の藩主」として扱う
    # (スタブの父をさらに遡った先が他藩の藩主だった場合にも連鎖を止められるようにする)
    known_daimyo_ids = id_set | load_known_daimyo_ids_from_raw_files()

    for idx, entry in enumerate(ordered_entries, start=1):
        name = entry.title
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

        # 在位年: テンプレート内の年数表記を優先し、なければWikidataのP39(役職)をフォールバックにする
        year_start, year_end = entry.year_start, entry.year_end
        if year_start is None and year_end is None and is_qid(node_id):
            year_start, year_end = get_wikidata_tenure_years(node_id, han_name)

        all_nodes[node_id] = DaimyoNode(
            id=node_id,
            name=name,
            positions=[{"han": han_name, "generation": idx, "year_start": year_start, "year_end": year_end}],
            wikipedia_url=build_url(name),
            image_url=get_image_url_from_wikidata(node_id),
            father_id=father_id,
            is_stub=False,
        )

        # 父が対象藩の藩主一覧に含まれない場合はスタブノードとして追加
        # (スタブノード側でも、既知の藩主または藩主マスターリストに行き着くまで
        # 最大MAX_STUB_ANCESTOR_DEPTH世代分は遡る)
        if father_id and father_id not in id_set and father_id not in all_nodes:
            print(f"    -> 父({father_id})は{han_name}主一覧に含まれないため、スタブノードとして追加します")
            add_stub_node(father_id, all_nodes, known_daimyo_ids, master_ids, MAX_STUB_ANCESTOR_DEPTH)

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
    raw_args = sys.argv[1:]
    force_refresh_master = "--refresh-master-list" in raw_args
    args = [a for a in raw_args if a != "--refresh-master-list"]

    targets = select_target_hans(args)

    # 藩主マスターリストは全藩共通で1度だけ構築する(藩ごとの処理の前に済ませる)。
    master_ids = build_daimyo_master_list(force_refresh=force_refresh_master)

    written: list[tuple[str, Path, int]] = []
    for han_name, conf in targets.items():
        # 警告も藩ごとのファイルに分けて記録するため、藩の処理ごとにリセットする
        reset_warnings()
        nodes = process_han(han_name, conf["template"], master_ids)
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
    