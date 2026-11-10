"""공공데이터포털에서 임대주택 모집공고를 받아 data/notices.json 으로 정리합니다.

- LH 분양임대공고문 조회 서비스 (한국토지주택공사)
- 마이홈포털 공공주택 모집공고 조회 서비스 (국토교통부)

GitHub Actions 가 하루 여러 번 이 파일을 실행합니다.
인증키는 저장소 비밀값(Secrets) DATA_GO_KR_KEY 에서만 읽고, 화면이나 파일에 남기지 않습니다.
한쪽 데이터를 못 받아오면 그쪽은 지난번에 받은 공고를 그대로 둡니다.
표준 라이브러리만 사용합니다(설치할 것 없음).
"""
from __future__ import annotations

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

# 다음 단계 준비: 자동 수집을 만들기 위해 세 기관의 공고 목록 페이지를 저장해 둬요(사이트 화면에는 안 보여요)
SAMPLE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "debug")
SAMPLE_PAGES = [
    ("youth_list", "https://soco.seoul.go.kr/youth/bbs/BMSR00015/list.do?menuNo=400008"),
    ("sh_housing_01", "https://housing.seoul.go.kr/site/main/sh/publicLease/01/list"),
    ("sh_housing_02", "https://housing.seoul.go.kr/site/main/sh/publicLease/02/list"),
    ("sh_housing_03", "https://housing.seoul.go.kr/site/main/sh/publicLease/03/list"),
    ("sh_housing_04", "https://housing.seoul.go.kr/site/main/sh/publicLease/04/list"),
    ("sh_ish_list", "https://www.i-sh.co.kr/main/lay2/program/S1T294C297/www/brd/m_247/list.do"),
    ("gh_main", "https://apply.gh.or.kr/co/coa/selectMainView.do"),
]
SAMPLE_LINK_PATTERNS = {
    "youth_list": r'href="([^"]*BMSR00015/view\.do[^"]*)"',
    "sh_housing_04": r'href="([^"]*publicLease/04/view[^"]*)"',
    "sh_ish_list": r'href="([^"]*m_247/view\.do[^"]*)"',
    "gh_main": r'(?:href|data-url)="([^"]*(?:Pbanc|pbanc|Rent|rent)[^"]*)"',
}
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
    young = "청년" in s
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
    # 제목·일정·지역이 똑같은 공고가 따로 올라온 경우 화면에는 한 번만 보여줘요
    seen, unique = set(), []
    for n in merged:
        k = (n["title"], n.get("start"), n.get("end"), n.get("regionText"))
        if k in seen:
            continue
        seen.add(k)
        unique.append(n)
    merged = [n for n in unique if is_target(n)]
    for n in merged:
        n["youth"] = "청년" in n["title"] or "청년" in (n.get("lhType") or "") or n["category"] == "행복주택"
    merged.sort(key=lambda n: (n.get("end") or "9999", n["title"]))
    return merged


def _get_raw(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept-Language": "ko-KR,ko;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.geturl(), r.read(), ""
    except urllib.error.HTTPError as e:
        return e.code, url, e.read() or b"", f"HTTP {e.code}"
    except Exception as e:
        if "CERTIFICATE_VERIFY_FAILED" in str(e):  # 인증서 설정이 덜 된 공공 사이트용: 읽기만 하므로 한 번 더 시도
            ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
                return r.status, r.geturl(), r.read(), "인증서 확인 생략"
        raise


def save_sample_pages():
    """세 기관의 공고 목록(과 첫 공고 몇 개)을 data/debug 에 저장해요. 실패해도 공고 업데이트에는 영향 없어요."""
    os.makedirs(SAMPLE_DIR, exist_ok=True)
    report = {"savedAt": datetime.now(KST).isoformat(timespec="minutes"), "pages": []}
    todo = list(SAMPLE_PAGES)
    done = 0
    while todo and done < 16:
        name, url = todo.pop(0)
        done += 1
        item = {"name": name, "url": url}
        try:
            status, final, body, note = _get_raw(url)
            item.update(status=status, finalUrl=final, bytes=len(body), note=note)
            with open(os.path.join(SAMPLE_DIR, f"{name}.html"), "wb") as f:
                f.write(body)
            pat = SAMPLE_LINK_PATTERNS.get(name)
            if pat:
                text = body.decode("utf-8", "ignore")
                links = []
                for href in re.findall(pat, text):
                    full = urllib.parse.urljoin(final, href.replace("&amp;", "&"))
                    if full not in links:
                        links.append(full)
                item["foundLinks"] = links[:10]
                for i, link in enumerate(links[:2], 1):
                    todo.append((f"{name}_view{i}", link))
        except Exception as e:
            item["error"] = str(e)[:300]
        report["pages"].append(item)
        print(f"  페이지 저장 {name}: {item.get('status', '')} {item.get('error', '')}")
    with open(os.path.join(SAMPLE_DIR, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    try:  # 공고 목록과 함께 저장소에 올라가도록 미리 담아 둬요
        subprocess.run(["git", "add", SAMPLE_DIR], check=False, capture_output=True)
    except Exception:
        pass


def main():
    try:
        save_sample_pages()
    except Exception as e:
        print("  (페이지 저장은 건너뜀:", e, ")")
    key = get_key()
    try:
        old = json.load(open(OUT, encoding="utf-8"))
    except Exception:
        old = {"notices": [], "sources": {}}
    old_by_src = {"LH": [], "마이홈": []}
    for n in old.get("notices", []):
        for s in ("LH", "마이홈"):
            if s in n.get("source", ""):
                old_by_src[s].append(n)

    now = datetime.now(KST).isoformat(timespec="minutes")
    sources, results = {}, {}
    for name, fn in (("LH", fetch_lh), ("마이홈", fetch_myhome)):
        try:
            results[name] = fn(key)
            sources[name] = {"ok": True, "count": len(results[name]), "checkedAt": now}
            print(f"{name}: 공고 {len(results[name])}건")
        except Exception as e:
            msg = hide(str(e), key)
            prev = (old.get("sources") or {}).get(name) or {}
            sources[name] = {"ok": False, "error": msg, "checkedAt": now, "lastOkAt": prev.get("lastOkAt")}
            results[name] = [n for n in old_by_src[name] if (n.get("end") or "9999") >= today_kst().isoformat()]
            print(f"{name}: 실패 → 지난 데이터 {len(results[name])}건 유지 ({msg})")
        if sources[name]["ok"]:
            sources[name]["lastOkAt"] = now

    if not any(s["ok"] for s in sources.values()):
        print("두 곳 모두 받아오지 못했어요. 인증키가 맞는지, 활용신청 후 1~2시간이 지났는지 확인해 주세요.")

    notices = merge(results["LH"], results["마이홈"])
    out = {"updatedAt": now, "sources": sources, "count": len(notices), "notices": notices}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"저장 완료: 공고 {len(notices)}건")
    if not any(s["ok"] for s in sources.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
