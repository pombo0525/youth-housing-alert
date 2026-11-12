"""공공데이터포털에서 임대주택 모집공고를 받아 data/notices.json 으로 정리합니다.

- LH 분양임대공고문 조회 서비스 (한국토지주택공사)
- 마이홈포털 공공주택 모집공고 조회 서비스 (국토교통부)
- 청년안심주택(서울시), SH(서울주거포털), GH 청약센터의 공개 공고 목록 페이지

GitHub Actions 가 하루 여러 번 이 파일을 실행합니다.
인증키는 저장소 비밀값(Secrets) DATA_GO_KR_KEY 에서만 읽고, 화면이나 파일에 남기지 않습니다.
한쪽 데이터를 못 받아오면 그쪽은 지난번에 받은 공고를 그대로 둡니다.
표준 라이브러리만 사용합니다(설치할 것 없음).
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
LH_URL = "https://apis.data.go.kr/B552555/lhLeaseNoticeInfo1/lhLeaseNoticeInfo1"
MYHOME_URL = "https://apis.data.go.kr/1613000/HWSPR02/rsdtRcritNtcList"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "notices.json")

# LH 상위 분류: 06 임대주택, 13 주거복지(매입임대·전세임대·든든전세 등)
LH_UPPER_CODES = ("06", "13")
MAX_PAGES = 15
KEEP_CLOSED_DAYS = 0  # 마감일이 지난 공고는 바로 뺍니다

# 서울·경기 청년이 들어갈 수 있는 임대주택만 남겨요
TARGET_SIDO = {"서울", "경기", "전국"}
EXCLUDE_WORDS = ("기관공급", "기업체", "신혼부부", "신혼희망", "신생아", "고령자", "다자녀", "분양", "상가")

# 공식 데이터가 없는 곳은 공개된 공고 목록 페이지를 하루 4번 읽어 와요(robots.txt 허용 확인: 2026.11)
YOUTH_HOME_JSON = "https://soco.seoul.go.kr/youth/pgm/home/yohome/bbsListJson.json"   # 청년안심주택 모집공고 목록
YOUTH_HOME_VIEW = "https://soco.seoul.go.kr/youth/bbs/BMSR00015/view.do?boardId={id}&menuNo=400008"
SH_LIST = "https://housing.seoul.go.kr/site/main/sh/publicLease/{code}/list"            # 서울주거포털의 SH 공공임대 목록
SH_CODES = ("01", "02", "03", "04", "05", "06")
GH_PAGES = ("https://apply.gh.or.kr/co/coa/selectMainView.do",
            "https://apply.gh.or.kr/sb/sr/sr7150/selectPbancRentHouseList.do",
            "https://apply.gh.or.kr/sb/sr/sr7155/selectPbancRentHouseList.do")
GH_LIST_URL = "https://apply.gh.or.kr/sb/sr/sr7150/selectPbancRentHouseList.do"
SAMPLE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "debug")  # 1단계 임시 복사본(이제 지워요)
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

SIDO = [
    ("서울", "서울"), ("부산", "부산"), ("대구", "대구"), ("인천", "인천"), ("광주", "광주"),
    ("대전", "대전"), ("울산", "울산"), ("세종", "세종"), ("경기", "경기"), ("강원", "강원"),
    ("충청북", "충북"), ("충북", "충북"), ("충청남", "충남"), ("충남", "충남"),
    ("전라북", "전북"), ("전북", "전북"), ("전라남", "전남"), ("전남", "전남"),
    ("경상북", "경북"), ("경북", "경북"), ("경상남", "경남"), ("경남", "경남"), ("제주", "제주"),
]


# ------------------------------------------------------------------ 도우미
class ApiError(Exception):
    pass


def today_kst() -> date:
    return datetime.now(KST).date()


def get_key() -> str:
    key = os.environ.get("DATA_GO_KR_KEY", "").strip()
    if not key:
        sys.exit("인증키가 없어요. 저장소 Settings > Secrets and variables > Actions 에 DATA_GO_KR_KEY 를 넣어 주세요.")
    # 'Encoding' 키(%가 들어간 키)를 넣었어도 동작하게 한 번 풀어 둡니다
    return urllib.parse.unquote(key) if "%" in key else key


def hide(text: str, key: str) -> str:
    for form in {key, urllib.parse.quote(key, safe=""), urllib.parse.quote_plus(key)}:
        if form:
            text = text.replace(form, "***")
    return text


def http_json(url: str, params: dict, key: str):
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{url}?{q}", headers={"Accept": "application/json", "User-Agent": "youth-housing-alert"})
    last = None
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                text = r.read().decode("utf-8", "replace")
            break
        except Exception as e:  # 네트워크가 잠깐 끊기면 다시 시도
            last = e
    else:
        raise ApiError(hide(f"접속 실패: {last}", key))
    t = text.lstrip()
    if t.startswith("<"):
        msg = re.search(r"<returnAuthMsg>(.*?)</returnAuthMsg>|<resultMsg>(.*?)</resultMsg>|<errMsg>(.*?)</errMsg>", t)
        raise ApiError("API 오류: " + hide(next((g for g in (msg.groups() if msg else ()) if g), t[:200]), key))
    try:
        return json.loads(text)
    except ValueError:
        raise ApiError("API가 알 수 없는 형식으로 답했어요: " + hide(text[:200], key))


def ymd(value) -> str | None:
    """'20261027', '2026.10.27', '2026-10-27' → '2026-10-27'"""
    if value in (None, ""):
        return None
    digits = re.sub(r"\D", "", str(value))
    if len(digits) < 8:
        return None
    try:
        return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8])).isoformat()
    except ValueError:
        return None


def sido_of(text: str | None) -> list[str]:
    if not text:
        return []
    if "전국" in text:
        return ["전국"]
    found = []
    for word in re.split(r"[\s,/·]+", text):
        for prefix, short in SIDO:
            if word.startswith(prefix) and short not in found:
                found.append(short)
                break
        # '전남광주통합특별시'처럼 두 지역이 합쳐진 이름은 양쪽 모두에 보여줘요
        if word.endswith(("특별시", "광역시", "도")):
            for short in ("서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "전남", "전북", "경남", "경북", "충남", "충북"):
                if short in word and short not in found:
                    found.append(short)
    return found


def category_of(type_name: str, title: str) -> str:
    s = f"{type_name} {title}"
    if "행복주택" in s:
        return "행복주택"
    if "전세임대" in s:
        return "전세임대"
    if "매입" in s or "든든전세" in s:
        return "매입임대"
    if any(w in s for w in ("국민임대", "영구임대", "통합공공", "공공임대", "장기전세", "50년", "10년", "5년")):
        return "국민·공공임대"
    return "기타"


def is_target(n: dict) -> bool:
    """서울·경기(또는 전국) 공고이면서 청년이 지원할 수 있는 임대주택인지"""
    if not (set(n.get("sido") or []) & TARGET_SIDO):
        return False
    s = f"{n.get('title', '')} {n.get('lhType') or ''}"
    young = "청년" in s or n.get("category") == "청년안심주택(서울)"
    if not young and any(w in s for w in EXCLUDE_WORDS):
        return False
    return young or n.get("category") == "행복주택"


def clean(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def is_closed_title(title: str) -> bool:
    """제목에 '조기접수마감', '모집완료'처럼 이미 끝났다고 적힌 공고"""
    return any(w in title for w in ("조기접수마감", "조기마감", "접수마감]", "모집완료", "모집마감]"))


# ------------------------------------------------------------------ LH
def fetch_lh(key: str) -> list[dict]:
    today = today_kst()
    rows = []
    for code in LH_UPPER_CODES:
        page = 1
        while page <= MAX_PAGES:
            data = http_json(LH_URL, {
                "serviceKey": key, "PG_SZ": "100", "PAGE": str(page), "UPP_AIS_TP_CD": code,
                "PAN_NT_ST_DT": (today - timedelta(days=90)).strftime("%Y.%m.%d"),
                "CLSG_DT": (today + timedelta(days=180)).strftime("%Y.%m.%d"),
            }, key)
            items = []
            blocks = data if isinstance(data, list) else [data]
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if isinstance(block.get("dsList"), list):
                    items = block["dsList"]
                for v in [block, *[x for val in block.values() if isinstance(val, list) for x in val if isinstance(x, dict)]]:
                    if v.get("SS_CODE") not in (None, "Y"):
                        raise ApiError("LH API 오류: " + clean(v.get("RS_MSG") or v.get("ERR_MSG") or v))
            if not items:
                break
            rows.extend(items)
            total = int(items[0].get("ALL_CNT") or 0)
            if page * 100 >= total:
                break
            page += 1

    out = {}
    for r in rows:
        end = ymd(r.get("CLSG_DT"))
        status = clean(r.get("PAN_SS"))
        if end and end < (today - timedelta(days=KEEP_CLOSED_DAYS)).isoformat():
            continue
        if status in ("접수마감", "당첨자발표"):
            continue
        pid = clean(r.get("PAN_ID"))
        title = clean(r.get("PAN_NM"))
        if is_closed_title(title):
            continue
        tname = clean(r.get("AIS_TP_CD_NM"))
        out[pid] = {
            "id": f"lh-{pid}", "panId": pid, "title": title, "source": "LH", "agency": "LH",
            "type": tname, "lhType": tname, "category": category_of(tname, title),
            "regionText": clean(r.get("CNP_CD_NM")), "sido": sido_of(r.get("CNP_CD_NM")),
            "noticeDate": ymd(r.get("PAN_NT_ST_DT")), "start": None, "end": end,
            "status": status, "url": clean(r.get("DTL_URL")),
        }
    return list(out.values())


# ------------------------------------------------------------------ 마이홈
def _myhome_pages(key: str, extra: dict) -> list[dict]:
    rows, page = [], 1
    while page <= MAX_PAGES:
        data = http_json(MYHOME_URL, {"serviceKey": key, "pageNo": str(page), "numOfRows": "100", **extra}, key)
        resp = data.get("response") if isinstance(data, dict) else None
        if not isinstance(resp, dict):
            raise ApiError("마이홈 API가 알 수 없는 형식으로 답했어요")
        head, body = resp.get("header") or {}, resp.get("body") or {}
        if str(head.get("resultCode", "00")) not in ("00", "0", "000"):
            raise ApiError("마이홈 API 오류: " + clean(head.get("resultMsg")))
        items = body.get("item")
        if items is None:
            items = body.get("items")
            if isinstance(items, dict):
                items = items.get("item")
        items = items or []
        if isinstance(items, dict):
            items = [items]
        if not items:
            break
        rows.extend(items)
        if page * 100 >= int(body.get("totalCount") or 0):
            break
        page += 1
    return rows


def fetch_myhome(key: str) -> list[dict]:
    today = today_kst()
    rows = _myhome_pages(key, {})
    # 기간 조건을 주면 더 많은 공고가 나오는 경우가 있어 한 번 더 물어봅니다(안 되면 그냥 넘어감)
    try:
        prev = (today.replace(day=1) - timedelta(days=1)).strftime("%Y%m")
        nxt = (today.replace(day=28) + timedelta(days=40)).strftime("%Y%m")
        rows += _myhome_pages(key, {"yearMtBegin": prev, "yearMtEnd": nxt})
    except ApiError as e:
        print("  (마이홈 기간 조회는 건너뜀:", e, ")")

    grouped: dict[str, dict] = {}
    for r in rows:
        pid = clean(r.get("pblancId"))
        k = pid  # 한 공고에 단지(houseSn)가 여러 개여도 공고 하나로 묶어요
        end = ymd(r.get("endDe"))
        if end and end < today.isoformat():
            continue
        title = clean(r.get("pblancNm"))
        if is_closed_title(title):
            continue
        tname = clean(r.get("suplyTyNm"))
        sido_full, sigungu = clean(r.get("brtcNm")), clean(r.get("signguNm"))
        g = grouped.get(k)
        if g is None:
            lh_url = clean(r.get("url"))
            m = re.search(r"panId=(\d+)", lh_url)
            g = grouped[k] = {
                "id": f"myhome-{k}", "panId": m.group(1) if m else None, "title": title, "source": "마이홈",
                "agency": clean(r.get("suplyInsttNm")) or "-", "type": tname, "category": category_of(tname, title),
                "houseType": clean(r.get("houseTyNm")), "_places": {}, "sido": [],
                "noticeDate": ymd(r.get("rcritPblancDe")), "start": ymd(r.get("beginDe")), "end": end,
                "winnerDate": ymd(r.get("przwnerPresnatnDe")), "status": clean(r.get("sttusNm")),
                "url": clean(r.get("pcUrl")) or lh_url, "agencyUrl": lh_url,
            }
        else:
            # 같은 공고의 다른 단지: 접수 기간은 가장 이른 시작 ~ 가장 늦은 마감으로 합쳐요
            s2 = ymd(r.get("beginDe"))
            if s2 and (not g["start"] or s2 < g["start"]):
                g["start"] = s2
            if end and (not g["end"] or end > g["end"]):
                g["end"] = end
        for s in sido_of(sido_full):
            if s not in g["sido"]:
                g["sido"].append(s)
        if sido_full:
            g["_places"].setdefault(sido_full, [])
            if sigungu and sigungu not in g["_places"][sido_full]:
                g["_places"][sido_full].append(sigungu)
    out = []
    for g in grouped.values():
        places = g.pop("_places")
        g["regionText"] = " / ".join(f"{s} {', '.join(v)}".strip() for s, v in places.items())
        out.append(g)
    return out


# ------------------------------------------------------------------ 합치기
def merge(lh: list[dict], myhome: list[dict]) -> list[dict]:
    """같은 LH 공고가 두 곳에 다 있으면 하나로 합칩니다(마이홈은 접수 시작일까지 알려줌)."""
    by_pan = {n["panId"]: n for n in lh if n.get("panId")}
    merged = []
    for m in myhome:
        l = by_pan.pop(m.get("panId"), None) if m.get("panId") else None
        if l:
            m = {**m, "source": "LH·마이홈", "status": l["status"] or m["status"], "lhType": l.get("lhType"),
                 "end": m["end"] or l["end"], "agencyUrl": l["url"] or m.get("agencyUrl")}
            if not m["sido"]:
                m["sido"] = l["sido"]
        merged.append(m)
    merged += list(by_pan.values())
    return merged


def finalize(notices: list[dict]) -> list[dict]:
    # 제목·일정·지역이 똑같은 공고가 따로 올라온 경우 화면에는 한 번만 보여줘요
    seen, unique = set(), []
    for n in notices:
        k = (n["title"], n.get("start"), n.get("end"), n.get("regionText"))
        if k in seen:
            continue
        seen.add(k)
        unique.append(n)
    merged = [n for n in unique if is_target(n)]
    for n in merged:
        n["youth"] = True
    merged.sort(key=lambda n: (n.get("end") or "9999", n["title"]))
    return merged


def http_raw(url: str, data: dict | None = None) -> str:
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    headers = {"User-Agent": BROWSER_UA, "Accept-Language": "ko-KR,ko;q=0.9"}
    if data is not None:
        headers.update({"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                        "X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/javascript, */*"})
    req = urllib.request.Request(url, data=body, headers=headers)
    last = None
    for _ in range(3):
        try:
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    raw = r.read()
            except Exception as e:
                if "CERTIFICATE_VERIFY_FAILED" not in str(e):
                    raise
                ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
                with urllib.request.urlopen(req, timeout=30, context=ctx) as r:  # 공개 페이지를 읽기만 해요
                    raw = r.read()
            return raw.decode("utf-8", "replace")
        except Exception as e:
            last = e
    raise ApiError(f"접속 실패: {last}")


def strip_tags(h: str) -> str:
    h = re.sub(r"<!--.*?-->", "", h, flags=re.S)
    return clean(html.unescape(re.sub(r"<[^>]+>", " ", h)))


def dates_in(text: str) -> tuple[str | None, str | None]:
    """'2026-11-05 ~ 2026-11-06', '2026.11.05(수)~11.06(목)', '2026.11.05' → (시작, 마감)"""
    text = text or ""
    m = re.search(r"(20\d\d)[.\-/년]\s*(\d{1,2})[.\-/월]\s*(\d{1,2})", text)
    if not m:
        return None, None
    y = int(m.group(1))
    try:
        start = date(y, int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None, None
    end = start
    rest = text[m.end():]
    if "~" in rest:
        after = rest.split("~", 1)[1]
        m2 = re.search(r"(?:(20\d\d)[.\-/년]\s*)?(\d{1,2})[.\-/월]\s*(\d{1,2})", after)
        if m2:
            try:
                end = date(int(m2.group(1) or y), int(m2.group(2)), int(m2.group(3)))
                if end < start:
                    end = date(end.year + 1, end.month, end.day)
            except ValueError:
                pass
    return start.isoformat(), end.isoformat()


# ------------------------------------------------------------------ 청년안심주택 (서울시)
# 목록의 '청약신청일' 칸에는 접수 시작일 하나만 적혀 있어요(2026.11 확인).
# 그래서 실제 접수 기간은 공고 상세 페이지에서 읽고, 못 읽으면 시작일만 보여줘요(마감일을 짐작하지 않아요).
PERIOD_WORDS = ("청약신청", "청약 신청", "청약접수", "청약 접수", "신청접수", "신청 접수", "접수기간", "접수 기간",
                "신청기간", "신청 기간", "모집기간", "모집 기간", "청약기간", "청약 기간", "청약일정", "인터넷청약", "인터넷 청약")
_D = r"(?:(20\d\d)\s*[.\-/년]\s*)?(\d{1,2})\s*[.\-/월]\s*(\d{1,2})\s*일?"
_SEP = r"\s*\.?\s*(?:\([^)]{1,4}\))?\s*(?:\d{1,2}:\d{2}\s*)?(?:[~∼〜～\-–]|부터)\s*"
RANGE_RE = re.compile(_D + _SEP + _D)
RANGE_DAY_RE = re.compile(_D + _SEP + r"(\d{1,2})\s*일?(?![\d.\-/월:])")   # '11.12(목)~15(일)'처럼 끝에 날짜만


def _mkdate(y, m, d, fallback_year):
    try:
        return date(int(y or fallback_year), int(m), int(d))
    except ValueError:
        return None


def find_period(text: str, start_hint: str | None, year: int) -> tuple[str | None, str | None, str]:
    """상세 페이지 글에서 '청약신청 2026.11.12(목) ~ 11.15(일)' 같은 접수 기간을 찾아요. (시작, 마감, 근거 문장)"""
    best = None
    hits = [(m, False) for m in RANGE_RE.finditer(text)] + [(m, True) for m in RANGE_DAY_RE.finditer(text)]
    hits.sort(key=lambda h: h[0].start())
    for m, day_only in hits:
        s = _mkdate(m.group(1), m.group(2), m.group(3), year)
        if not s:
            continue
        if day_only:
            e = _mkdate(s.year, s.month, m.group(4), s.year)
        else:
            e = _mkdate(m.group(4) or s.year, m.group(5), m.group(6), s.year)
        if not e:
            continue
        if e < s:
            e = _mkdate(e.year + 1, e.month, e.day, e.year + 1)
        if not e or (e - s).days > 31:
            continue
        before = text[max(0, m.start() - 70):m.start()]
        keyword = any(w in before for w in PERIOD_WORDS)
        hint = start_hint is not None and s.isoformat() == start_hint
        score = (2 if hint else 0) + (1 if keyword else 0)
        if score == 0:
            continue
        if best is None or score > best[0]:
            best = (score, s.isoformat(), e.isoformat(), clean(text[max(0, m.start() - 30):m.end()])[:120])
            if score == 3:
                break
    if best:
        return best[1], best[2], best[3]
    return None, None, ""


def fetch_youth_home() -> list[dict]:
    today = today_kst()
    cand = []
    for page in (1, 2, 3):
        text = http_raw(YOUTH_HOME_JSON, {"bbsId": "BMSR00015", "pageIndex": str(page), "searchAdresGu": "",
                                          "searchCondition": "", "searchKeyword": "", "optn2": "", "optn5": ""})
        try:
            data = json.loads(text)
        except ValueError:
            raise ApiError("청년안심주택 목록이 알 수 없는 형식으로 왔어요: " + clean(text[:120]))
        rows = data.get("resultList") or []
        if not rows:
            break
        stop = False
        for r in rows:
            posted = ymd(r.get("optn1"))
            if posted and posted < (today - timedelta(days=60)).isoformat():
                stop = True  # 두 달 넘은 공고부터는 더 보지 않아요
            raw = str(r.get("optn4") or "")
            start, list_end = dates_in(raw)
            if list_end == start and "~" not in raw:
                list_end = None          # 날짜가 하나뿐이면 시작일로만 써요
            if list_end and list_end < today.isoformat():
                continue
            if not list_end:
                ref = start or posted
                if not ref or ref < (today - timedelta(days=21)).isoformat():
                    continue             # 3주 넘게 지난 공고는 이미 끝났을 가능성이 커서 빼요
            cand.append((r, posted, start, list_end))
        if stop:
            break

    out, found, missed = [], 0, []
    for r, posted, start, end in cand:
        url = YOUTH_HOME_VIEW.format(id=r.get("boardId"))
        evidence = ""
        if not end:
            try:
                page_text = strip_tags(http_raw(url))
                year = int((start or posted or today.isoformat())[:4])
                s2, e2, evidence = find_period(page_text, start, year)
                if e2:
                    start, end = s2, e2
                    found += 1
                else:
                    i = max(page_text.find("청약"), 0)
                    missed.append({"id": r.get("boardId"), "near": page_text[i:i + 160]})
            except ApiError as e:
                missed.append({"id": r.get("boardId"), "near": f"상세 페이지 접속 실패: {e}"[:160]})
        if end and end < today.isoformat():
            continue
        if not end and start and start < (today - timedelta(days=7)).isoformat():
            continue  # 마감일을 모르는 공고는 시작 7일 뒤 목록에서 빼요(청년안심주택 접수는 보통 2~5일)
        kind = {"1": "공공", "2": "민간"}.get(str(r.get("optn2")), "")
        extra = {"1": "최초", "2": "추가"}.get(str(r.get("optn5")), "")
        out.append({
            "id": f"youthhome-{r.get('boardId')}", "title": clean(r.get("nttSj")), "source": "청년안심주택",
            "agency": clean(r.get("optn3")) or "서울시", "type": " ".join(x for x in ("청년안심주택", kind, extra) if x),
            "category": "청년안심주택(서울)", "regionText": "서울특별시", "sido": ["서울"],
            "noticeDate": posted, "start": start, "end": end, "status": "", "url": url,
            "periodFrom": "상세 페이지" if evidence else ("목록" if end else "시작일만"),
        })
    YOUTH_DETAIL_REPORT.update({"periodFound": found, "periodMissed": missed[:10]})
    return out


YOUTH_DETAIL_REPORT: dict = {}


# ------------------------------------------------------------------ SH (서울주거포털)
def fetch_sh() -> list[dict]:
    out, pages_ok = [], 0
    for code in SH_CODES:
        try:
            t = http_raw(SH_LIST.format(code=code))
        except ApiError:
            continue
        if "<tbody" not in t:
            continue
        pages_ok += 1
        tbody = t[t.find("<tbody"):t.find("</tbody>")]
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", tbody, re.S):
            tr = re.sub(r"<!--.*?-->", "", tr, flags=re.S)
            tds = [strip_tags(td) for td in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
            if len(tds) < 7:
                continue
            _, kind, title, posted, announce, status = tds[:6]
            if status != "모집중":
                continue
            link = re.search(r'href="(https?://[^"]+)"', tr)
            out.append({
                "id": "sh-" + code + "-" + hashlib.md5(title.encode()).hexdigest()[:10], "title": title, "source": "SH", "agency": "SH",
                "type": kind, "category": category_of(kind, title), "regionText": "서울특별시", "sido": ["서울"],
                "noticeDate": ymd(posted), "start": None, "end": None, "winnerDate": ymd(announce),
                "status": "모집중", "url": link.group(1) if link else SH_LIST.format(code=code),
            })
    if not pages_ok:
        raise ApiError("SH 공고 목록 페이지를 하나도 읽지 못했어요")
    return out


# ------------------------------------------------------------------ GH (경기주택도시공사)
def fetch_gh() -> list[dict]:
    today = today_kst()
    found, pages_ok = {}, 0
    for url in GH_PAGES:
        try:
            t = http_raw(url)
        except ApiError:
            continue
        hits = list(re.finditer(r'<a [^>]*data-pbancNo="(\d+)"[^>]*>(.*?)</a>', t, re.S))
        if hits:
            pages_ok += 1
        for m in hits:
            no, tag, body = m.group(1), m.group(0), m.group(2)
            if no in found:
                continue
            biz = re.search(r'data-bizTyNm="([^"]*)"', tag)
            title = re.search(r'<p class="leading-6[^"]*">(.*?)</p>', body, re.S)
            period = re.search(r"신청기간\s*([0-9.\-]*)\s*~\s*([0-9.\-]*)", body)
            st = re.search(r'status-[a-z]+[^>]*>\s*([^<]*?)\s*<', body)
            found[no] = (clean(biz.group(1)) if biz else "", strip_tags(title.group(1)) if title else "",
                         period.groups() if period else ("", ""), clean(st.group(1)) if st else "")
    if not pages_ok:
        raise ApiError("GH 공고 목록을 읽지 못했어요")
    out = []
    for no, (biz, title, (s1, s2), status) in found.items():
        start, end = ymd(s1), ymd(s2) or ymd(s1)
        if "마감" in status or (end and end < today.isoformat()):
            continue
        if not end and status != "공고중":
            continue
        out.append({
            "id": f"gh-{no}", "title": title, "source": "GH", "agency": "GH", "type": biz,
            "category": category_of(biz, title), "regionText": "경기도", "sido": ["경기"],
            "noticeDate": None, "start": start, "end": ymd(s2), "status": status, "url": GH_LIST_URL,
        })
    return out


def remove_sample_pages():
    """1단계에서 임시로 저장한 페이지 복사본을 지워요."""
    if os.path.isdir(SAMPLE_DIR):
        import shutil
        shutil.rmtree(SAMPLE_DIR, ignore_errors=True)
        try:
            subprocess.run(["git", "rm", "-r", "-q", "--cached", "--ignore-unmatch", SAMPLE_DIR], check=False, capture_output=True)
        except Exception:
            pass


SOURCES = (("LH", None), ("마이홈", None), ("청년안심주택", fetch_youth_home), ("SH", fetch_sh), ("GH", fetch_gh))


def main():
    remove_sample_pages()
    key = get_key()
    try:
        old = json.load(open(OUT, encoding="utf-8"))
    except Exception:
        old = {"notices": [], "sources": {}}
    names = [n for n, _ in SOURCES]
    old_by_src = {n: [] for n in names}
    for n in old.get("notices", []):
        for s in names:
            if s in n.get("source", "").split("·"):
                old_by_src[s].append(n)

    now = datetime.now(KST).isoformat(timespec="minutes")
    sources, results = {}, {}
    for name, fn in SOURCES:
        try:
            if name == "LH":
                results[name] = fetch_lh(key)
            elif name == "마이홈":
                results[name] = fetch_myhome(key)
            else:
                results[name] = fn()
            sources[name] = {"ok": True, "count": len(results[name]), "checkedAt": now, "lastOkAt": now}
            if name == "청년안심주택":
                sources[name].update(YOUTH_DETAIL_REPORT)   # 접수 기간을 몇 건 읽었는지(확인용)
            print(f"{name}: 공고 {len(results[name])}건")
        except Exception as e:
            msg = hide(str(e), key)
            prev = (old.get("sources") or {}).get(name) or {}
            sources[name] = {"ok": False, "error": msg, "checkedAt": now, "lastOkAt": prev.get("lastOkAt")}
            results[name] = [n for n in old_by_src[name] if (n.get("end") or "9999") >= today_kst().isoformat()]
            print(f"{name}: 실패 → 지난 데이터 {len(results[name])}건 유지 ({msg})")

    if not any(sources[n]["ok"] for n in names):
        print("모든 곳에서 받아오지 못했어요. 인증키와 인터넷 연결을 확인해 주세요.")

    notices = finalize(merge(results["LH"], results["마이홈"]) + results["청년안심주택"] + results["SH"] + results["GH"])
    out = {"updatedAt": now, "sources": sources, "count": len(notices), "notices": notices}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"저장 완료: 서울·경기 청년임대주택 공고 {len(notices)}건")
    if not any(sources[n]["ok"] for n in names):
        sys.exit(1)


if __name__ == "__main__":
    main()
