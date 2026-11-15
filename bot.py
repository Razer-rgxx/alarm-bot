"""
Gemini 알림봇
- 디스코드 채널에 "OO할 때 알려줘" 같은 요청을 쓰면 Gemini가 감시 계획으로 바꿔 저장
- 주기적으로 Gemini + 구글 검색으로 일정을 찾아 디스코드로 알림
- GitHub Actions에서 10분마다 한 번씩 실행되는 구조 (상태는 state.json에 저장)
"""
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import requests

KST = timezone(timedelta(hours=9))
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL") or "gemini-3.5-flash"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL") or "claude-haiku-5-5"
DISCORD_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID", "")

DISCORD_API = "https://discord.com/api/v10"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

HELP_TEXT = (
    "**🔔 알림봇 사용법**\n"
    "• 그냥 원하는 걸 말하세요 → 감시 등록\n"
    "  예) `마인크래프트 유튜버 코마가 시참할 때 알려줘`\n"
    "  예) `아이유 콘서트 티켓팅 열리면 하루 전에 알려줘`\n"
    "• `목록` → 등록된 감시 보기\n"
    "• `삭제 2` → 2번 감시 삭제\n"
    "• `확인 2` → 2번 감시를 다음 실행 때 바로 검색\n"
    "• `도움말` → 이 안내\n"
    "_(봇은 약 10분마다 깨어나서 답장하니 조금 기다려 주세요)_"
)


def now_kst() -> datetime:
    return datetime.now(KST)


# ───────────────────────── 상태 저장 ─────────────────────────
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"last_message_id": None, "next_id": 1, "watches": []}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ───────────────────────── 디스코드 ─────────────────────────
def discord_headers() -> dict:
    return {
        "Authorization": f"Bot {DISCORD_TOKEN}",
        "User-Agent": "DiscordBot (https://github.com, 1.0)",
        "Content-Type": "application/json",
    }


def fetch_new_messages(after_id):
    params = {"limit": 50}
    if after_id:
        params["after"] = after_id
    r = requests.get(f"{DISCORD_API}/channels/{CHANNEL_ID}/messages",
                     headers=discord_headers(), params=params, timeout=30)
    r.raise_for_status()
    msgs = r.json()
    msgs.sort(key=lambda m: int(m["id"]))  # 오래된 것부터
    return msgs


def send(text: str, reply_to: str = None) -> None:
    for i in range(0, len(text), 1900):  # 디스코드 2000자 제한
        body = {"content": text[i:i + 1900], "allowed_mentions": {"parse": []}}
        if reply_to and i == 0:
            body["message_reference"] = {"message_id": reply_to, "fail_if_not_exists": False}
        r = requests.post(f"{DISCORD_API}/channels/{CHANNEL_ID}/messages",
                          headers=discord_headers(), json=body, timeout=30)
        if r.status_code >= 400:
            print("디스코드 전송 실패:", r.status_code, r.text, file=sys.stderr)


# ───────────────────────── Gemini ─────────────────────────
def claude(prompt: str, search: bool = False) -> str:
    body = {
        "model": CLAUDE_MODEL,
        "max_tokens": 2000,
        "messages": [{"role": "user", "content": prompt}],
    }
    if search:
        body["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
    r = requests.post("https://api.anthropic.com/v1/messages",
                      headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                               "content-type": "application/json"},
                      json=body, timeout=180)
    if r.status_code >= 400:
        try:
            detail = r.json().get("error", {}).get("message", r.text)
        except ValueError:
            detail = r.text
        raise RuntimeError(f"Claude 오류 {r.status_code}: {detail[:300]}")
    blocks = r.json().get("content", [])
    return "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")


def gemini(prompt: str, search: bool = False, json_mode: bool = False) -> str:
    if ANTHROPIC_API_KEY:  # Claude 키가 있으면 Claude 사용
        return claude(prompt, search=search)
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    if search:
        body["tools"] = [{"google_search": {}}]
    if json_mode:
        body["generationConfig"] = {"responseMimeType": "application/json"}
    r = requests.post(GEMINI_URL.format(model=GEMINI_MODEL),
                      headers={"x-goog-api-key": GEMINI_API_KEY},
                      json=body, timeout=180)
    if r.status_code >= 400:
        try:
            detail = r.json().get("error", {}).get("message", r.text)
        except ValueError:
            detail = r.text
        raise RuntimeError(f"Gemini 오류 {r.status_code}: {detail[:300]}")
    data = r.json()
    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts)


def extract_json(text: str):
    """모델 답변에서 JSON 부분만 꺼내기 (```json 블록이나 섞인 문장 대응)"""
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        s, e = text.find(open_c), text.rfind(close_c)
        if s != -1 and e > s:
            try:
                return json.loads(text[s:e + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("JSON을 찾지 못함: " + text[:200])


PLAN_PROMPT = """너는 사용자의 알림 요청을 '감시 계획'으로 바꾸는 도우미야.
현재 시각(한국): {now}

사용자 요청: "{request}"

아래 JSON 하나만 출력해:
{{
  "title": "짧은 이름 (예: 코마 시참)",
  "search_query": "이 일정을 찾을 때 구글에 검색할 핵심 검색어 (한국어, 필요하면 영어 병기)",
  "event_description": "어떤 일이 생기면 알림 대상인지 구체적으로 (예: 유튜버 코마가 시청자 참여 마인크래프트 방송을 예고하거나 시작함)",
  "where_to_look": "주로 확인할 곳 (예: 유튜브 커뮤니티 탭, 치지직, 트위터/X, 카페 공지)",
  "check_every_hours": 검색 주기(시간, 1~24 정수. 방송처럼 급한 건 1~2, 콘서트·출시처럼 느린 건 6~24),
  "remind_before_minutes": 시작 몇 분 전에 다시 알릴지 (요청에 말이 없으면 30),
  "understood": "사용자에게 보여줄 한 줄 확인 문장 (예: 코마님이 시참을 예고하면 바로, 시작 30분 전에 한 번 더 알려드릴게요)",
  "youtube_channels": ["요청이 특정 유튜버의 방송·라이브·시참·커뮤니티 공지에 관한 거면 그 유튜버 이름들 (예: ["코마", "조밈"]). 아니면 빈 목록"],
  "channel_hint": "유튜버를 찾을 때 덧붙일 단어 (예: 마인크래프트). 없으면 빈 문자열",
  "keywords": ["방송 제목·공지에서 찾을 단어. 시참이면 ["시참", "시청자 참여", "시청자참여"]. 사용자가 모든 방송을 원하면 빈 목록"]
}}"""

SEARCH_PROMPT = """너는 일정 감시 도우미야. 구글 검색을 사용해서 최신 정보를 찾아줘.
현재 시각(한국): {now}

감시 대상: {title}
알림 조건: {event_description}
주로 확인할 곳: {where_to_look}
추천 검색어: {search_query}

지금 진행 중이거나 앞으로 14일 안에 예정된, 알림 조건에 맞는 일정을 찾아.
- 실제 출처(공지, 게시글, 방송 예약 등)에서 확인된 것만. 추측·지난 일정은 빼.
- 시간은 한국시간 ISO 8601 형식(예: 2026-11-20T20:00:00+09:00). 날짜만 알면 시간은 00:00:00, 모르면 null.
- 없으면 빈 목록.

반드시 아래 JSON만 출력 (설명 문장 금지):
```json
{{"events": [
  {{"title": "일정 이름", "start": "ISO 시각 또는 null", "is_live_now": false,
    "summary": "한 줄 요약", "url": "출처 링크 또는 null", "confidence": "high|medium|low"}}
]}}
```"""


def make_plan(request: str) -> dict:
    text = gemini(PLAN_PROMPT.format(now=now_kst().isoformat(timespec="minutes"), request=request),
                  json_mode=True)
    plan = extract_json(text)
    plan["check_every_hours"] = max(1, min(24, int(plan.get("check_every_hours") or 2)))
    plan["remind_before_minutes"] = max(0, int(plan.get("remind_before_minutes") or 30))
    return plan


def free_search(query: str) -> str:
    """DuckDuckGo 무료 검색 (키·카드 불필요) → 결과를 글로 정리"""
    from ddgs import DDGS
    lines, seen = [], set()
    with DDGS() as d:
        for kind in ("news", "text"):
            try:
                rs = (d.news if kind == "news" else d.text)(query, region="kr-kr", max_results=8)
            except Exception as e:
                print(f"DuckDuckGo {kind} 검색 실패: {e}", file=sys.stderr)
                continue
            for r in rs or []:
                url = r.get("url") or r.get("href") or ""
                if url in seen:
                    continue
                seen.add(url)
                date = f" ({r['date']})" if r.get("date") else ""
                lines.append(f"- {r.get('title', '')}{date}\n  {r.get('body', '')[:300]}\n  {url}")
    if not lines:
        raise RuntimeError("DuckDuckGo 검색 결과 없음")
    return "\n".join(lines[:16])


def search_events(watch: dict) -> list:
    prompt = SEARCH_PROMPT.format(now=now_kst().isoformat(timespec="minutes"), **{
        k: watch.get(k, "") for k in ("title", "event_description", "where_to_look", "search_query")
    })
    if ANTHROPIC_API_KEY:
        text = gemini(prompt, search=True)  # Claude 웹 검색
    else:
        try:  # 무료: DuckDuckGo로 검색 + Gemini는 정리만 (구글 검색 한도 안 씀)
            results = free_search(watch.get("search_query") or watch["title"])
            text = gemini(prompt.replace("구글 검색을 사용해서 최신 정보를 찾아줘.",
                                         "아래 검색 결과만 보고 판단해줘.")
                          + "\n\n[검색 결과]\n" + results)
        except ImportError:
            text = gemini(prompt, search=True)
    data = extract_json(text)
    events = data.get("events", []) if isinstance(data, dict) else data
    return [e for e in events if isinstance(e, dict) and e.get("title")]


# ───────────────────────── 유튜브 (무료, API 키 불필요) ─────────────────────────
YT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Cookie": "CONSENT=YES+1; SOCS=CAI; PREF=tz=Asia.Seoul&hl=ko&gl=KR",  # 시간을 한국시간으로
}
SICHAM_WORDS = ["시참", "시청자 참여", "시청자참여"]


def yt_page(url: str, var: str = "ytInitialData") -> dict:
    r = requests.get(url, headers=YT_HEADERS, timeout=30)
    r.raise_for_status()
    m = re.search(var + r" = (\{.*?\});(?:var |</script>)", r.text, re.S)
    if not m:
        raise RuntimeError(f"유튜브 페이지 해석 실패 ({url})")
    return json.loads(m.group(1))


def yt_find(obj, key, out=None):
    out = [] if out is None else out
    if isinstance(obj, dict):
        if key in obj:
            out.append(obj[key])
        for v in obj.values():
            yt_find(v, key, out)
    elif isinstance(obj, list):
        for v in obj:
            yt_find(v, key, out)
    return out


def yt_channel_url(handle: str) -> str:
    return "https://www.youtube.com/@" + requests.utils.quote(handle.lstrip("@"))


def yt_resolve(name: str, hint: str = ""):
    """이름으로 유튜브 채널 찾기 → (핸들, 채널명, 구독자)"""
    q = requests.utils.quote(f"{name} {hint}".strip())
    data = yt_page(f"https://www.youtube.com/results?sp=EgIQAg%253D%253D&search_query={q}")
    chans = yt_find(data, "channelRenderer")
    if not chans:
        return None
    def info(c):
        url = c.get("navigationEndpoint", {}).get("browseEndpoint", {}).get("canonicalBaseUrl", "")
        subs = (c.get("videoCountText", {}).get("simpleText")
                or c.get("subscriberCountText", {}).get("simpleText") or "")
        return requests.utils.unquote(url).lstrip("/"), c["title"]["simpleText"], subs
    exact = [c for c in chans if c["title"]["simpleText"].replace(" ", "") == name.replace(" ", "")]
    return info((exact or chans)[0])


def yt_streams(handle: str) -> list:
    """채널의 라이브/예정 방송 목록: [{id, title, status: live|upcoming}]"""
    data = yt_page(yt_channel_url(handle) + "/streams")
    out = []
    for l in yt_find(data, "lockupViewModel"):
        vid = l.get("contentId")
        try:
            title = l["metadata"]["lockupMetadataViewModel"]["title"]["content"]
        except (KeyError, TypeError):
            continue
        s = json.dumps(l, ensure_ascii=False)
        if "BADGE_STYLE_LIVE" in s:
            out.append({"id": vid, "title": title, "status": "live"})
        elif '"text": "예정"' in s or "UPCOMING" in s:
            start = None
            m = re.search(r"예정일: (\d{2})\. (\d{1,2})\. (\d{1,2})\. (\d{1,2}):(\d{2})", s)
            if m:
                y, mo, d, h, mi = map(int, m.groups())
                start = datetime(2000 + y, mo, d, h, mi, tzinfo=KST)
            out.append({"id": vid, "title": title, "status": "upcoming", "start": start})
    return out


def yt_start_time(video_id: str):
    """예정 방송의 시작 시각 (한국시간)"""
    try:
        p = yt_page(f"https://www.youtube.com/watch?v={video_id}", "ytInitialPlayerResponse")
        ts = yt_find(p, "scheduledStartTime")
        if ts:
            return datetime.fromtimestamp(int(ts[0]), KST)
    except Exception as e:
        print("시작 시각 확인 실패:", e, file=sys.stderr)
    return None


def yt_posts(handle: str) -> list:
    data = yt_page(yt_channel_url(handle) + "/posts")
    out = []
    for p in yt_find(data, "backstagePostRenderer"):
        text = "".join(r.get("text", "") for r in p.get("contentText", {}).get("runs", []))
        out.append({"id": p.get("postId"), "text": text})
    return out


def matches(text: str, keywords: list) -> bool:
    if not keywords:
        return True
    t = text.replace(" ", "").lower()
    return any(k.replace(" ", "").lower() in t for k in keywords)


def run_youtube(watch: dict) -> None:
    """10분마다 실행: 라이브 시작, 예약 방송, 커뮤니티 공지 확인"""
    handle, kw, now = watch["channel"], watch.get("keywords", []), now_kst()
    first = not watch.get("initialized")
    last_posts = parse_time(watch.get("posts_checked"))
    check_posts = first or not last_posts or now - last_posts >= timedelta(minutes=25)
    try:
        streams = yt_streams(handle)
        posts = yt_posts(handle) if check_posts else []
    except Exception as e:
        err = str(e)[:300]
        watch["fails"] = watch.get("fails", 0) + 1
        print(f"[{watch['title']}] 유튜브 확인 실패({watch['fails']}): {err}", file=sys.stderr)
        if watch["fails"] == 3:  # 일시적 오류는 무시, 3번 연속 실패하면 알림
            send(f"⚠️ [{watch['id']}번 {watch['title']}] 유튜브 확인이 계속 실패해요\n```{err}```")
        return
    watch["fails"] = 0
    if check_posts:
        watch["posts_checked"] = now.isoformat()
    seen_live = watch.setdefault("seen_live", [])
    for s in streams:
        if not matches(s["title"], kw):
            continue
        url = f"https://youtu.be/{s['id']}"
        if s["status"] == "live" and s["id"] not in seen_live:
            seen_live.append(s["id"])
            ev = watch["events"].get(s["id"])
            if ev:
                ev["reminded"] = True
            send(f"🔴 **지금 라이브 중!** [{watch['title']}]\n**{s['title']}**\n🔗 {url}")
        elif s["status"] == "upcoming" and s["id"] not in watch["events"]:
            start = s.get("start") or yt_start_time(s["id"])
            watch["events"][s["id"]] = {"title": s["title"], "start": start.isoformat() if start else None,
                                        "url": url, "found_at": now.isoformat(), "reminded": False}
            send(f"📅 **방송 예약됨!** [{watch['title']}]\n**{s['title']}**\n🕒 {fmt_time(start)}\n🔗 {url}")
    seen_posts = watch.setdefault("seen_posts", [])
    for p in posts:
        if not p["id"] or p["id"] in seen_posts:
            continue
        seen_posts.append(p["id"])
        if not first and matches(p["text"], kw):
            send(f"📢 **새 커뮤니티 글** [{watch['title']}]\n{p['text'][:600]}\n"
                 f"🔗 https://www.youtube.com/post/{p['id']}")
    del seen_live[:-50], seen_posts[:-50]
    watch["initialized"] = True


# ───────────────────────── 로직 ─────────────────────────
def parse_time(s):
    if not s:
        return None
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=KST)
    except ValueError:
        return None


def event_key(watch_id: int, ev: dict) -> str:
    t = parse_time(ev.get("start"))
    base = t.astimezone(KST).strftime("%Y-%m-%dT%H") if t else re.sub(r"\s+", "", ev["title"].lower())
    return hashlib.md5(f"{watch_id}|{base}".encode()).hexdigest()[:12]


def fmt_time(t) -> str:
    if not t:
        return "시간 미정"
    t = t.astimezone(KST)
    days = "월화수목금토일"
    return t.strftime(f"%m/%d({days[t.weekday()]}) %H:%M")


def handle_message(state: dict, msg: dict) -> None:
    raw = msg.get("content", "")
    text = re.sub(r"<@[!&]?\d+>", "", raw).strip()  # 멘션 제거
    mid = msg["id"]
    if not raw and not msg.get("attachments") and not msg.get("embeds"):
        if not state.get("_warned_intent"):
            state["_warned_intent"] = True
            send("⚠️ 메시지 내용을 읽을 수 없어요. 디스코드 개발자 사이트 → 봇(Bot) → "
                 "**MESSAGE CONTENT INTENT** 를 켜고 저장한 뒤 다시 보내 주세요.", mid)
        return
    if not text:
        return  # 멘션만 있는 메시지는 무시
    watches = state["watches"]

    if text in ("도움말", "help", "!help", "?"):
        send(HELP_TEXT, mid)
        return

    if text in ("목록", "리스트", "list"):
        if not watches:
            send("등록된 감시가 없어요. 원하는 걸 그냥 말해 주세요!", mid)
            return
        lines = ["**📋 감시 목록**"]
        for w in watches:
            upcoming = [e for e in w["events"].values() if not e.get("expired")]
            how = "유튜브 10분마다" if w.get("type") == "youtube" else f"{w['check_every_hours']}시간마다 검색"
            lines.append(f"`{w['id']}` **{w['title']}** — {how}, 알고 있는 일정 {len(upcoming)}개")
        send("\n".join(lines), mid)
        return

    m = re.fullmatch(r"(삭제|취소|delete)\s*(\d+)", text)
    if m:
        wid = int(m.group(2))
        before = len(watches)
        state["watches"] = [w for w in watches if w["id"] != wid]
        send(f"🗑️ {wid}번 감시를 삭제했어요." if len(state["watches"]) < before
             else f"{wid}번 감시가 없어요. `목록`으로 확인해 보세요.", mid)
        return

    m = re.fullmatch(r"(확인|check)\s*(\d+)", text)
    if m:
        wid = int(m.group(2))
        for w in watches:
            if w["id"] == wid:
                w["last_checked"] = None
                send(f"🔍 {wid}번 감시를 지금 바로 검색할게요.", mid)
                break
        else:
            send(f"{wid}번 감시가 없어요.", mid)
        return

    # 그 외는 전부 새 감시 요청
    handles = re.findall(r"(?:youtube\.com/)?@([^\s/?]+)", text)
    try:
        plan = make_plan(text)
    except Exception as e:
        if not handles:
            send(f"⚠️ 요청을 이해하는 중 오류가 났어요: {e}", mid)
            return
        plan = {"youtube_channels": [], "keywords": SICHAM_WORDS if "시참" in text else []}

    # 유튜버 방송 감시 → 무료 유튜브 확인 (10분마다)
    names = [n for n in plan.get("youtube_channels") or [] if isinstance(n, str) and n.strip()]
    if handles or names:
        keywords = plan.get("keywords")
        if not isinstance(keywords, list):
            keywords = SICHAM_WORDS if "시참" in text else []
        found = [(requests.utils.unquote(h), requests.utils.unquote(h), "") for h in handles]
        for n in names:
            if handles:
                break  # 주소를 직접 줬으면 그걸 사용
            try:
                r = yt_resolve(n, plan.get("channel_hint", ""))
            except Exception as e:
                r = None
                print("채널 검색 실패:", e, file=sys.stderr)
            if r:
                found.append(r)
            else:
                send(f"⚠️ '{n}' 유튜브 채널을 못 찾았어요. 채널 주소(youtube.com/@...)를 같이 보내 주세요.", mid)
        lines = []
        for handle, cname, subs in found:
            handle = handle.lstrip("@")
            watch = {"id": state["next_id"], "type": "youtube", "request": text,
                     "title": f"{cname} 방송", "channel": handle, "keywords": keywords,
                     "remind_before_minutes": plan.get("remind_before_minutes", 30),
                     "created": now_kst().isoformat(), "events": {}}
            state["next_id"] += 1
            watches.append(watch)
            lines.append(f"`{watch['id']}` **{cname}** (youtube.com/@{handle}) {subs}")
        if lines:
            what = "제목/공지에 " + ", ".join(f"'{k}'" for k in keywords) + " 들어간 방송" if keywords else "모든 방송"
            send("✅ **유튜브 감시 등록**\n" + "\n".join(lines) +
                 f"\n📺 {what}을 10분마다 확인해요 (라이브 시작 · 방송 예약 · 커뮤니티 공지)\n"
                 "_채널이 틀렸으면 `삭제 번호` 후 채널 주소를 같이 보내 주세요._", mid)
        return

    watch = {
        "id": state["next_id"],
        "request": text,
        "title": plan.get("title") or text[:30],
        "search_query": plan.get("search_query", text),
        "event_description": plan.get("event_description", text),
        "where_to_look": plan.get("where_to_look", ""),
        "check_every_hours": plan["check_every_hours"],
        "remind_before_minutes": plan["remind_before_minutes"],
        "created": now_kst().isoformat(),
        "last_checked": None,  # 다음 단계에서 바로 첫 검색
        "events": {},
    }
    state["next_id"] += 1
    watches.append(watch)
    send(f"✅ **{watch['id']}번 감시 등록: {watch['title']}**\n{plan.get('understood', '')}\n"
         f"🔎 검색어: `{watch['search_query']}` · {watch['check_every_hours']}시간마다 확인 · "
         f"시작 {watch['remind_before_minutes']}분 전 리마인드", mid)


def run_watch(watch: dict) -> None:
    now = now_kst()
    last = parse_time(watch.get("last_checked"))
    if last and now - last < timedelta(hours=watch["check_every_hours"], minutes=-5):
        return  # 아직 검색할 때 아님
    try:
        events = search_events(watch)
    except Exception as e:
        watch["last_checked"] = now.isoformat()  # 실패해도 다음 주기까지 기다림 (한도 낭비 방지)
        err = str(e)[:400]
        print(f"[{watch['title']}] 검색 실패: {err}", file=sys.stderr)
        if watch.get("last_error") != err:  # 같은 에러는 한 번만 알림
            watch["last_error"] = err
            send(f"⚠️ [{watch['id']}번 {watch['title']}] 검색 실패\n```{err}```")
        return
    watch["last_checked"] = now.isoformat()
    watch.pop("last_error", None)

    for ev in events:
        if ev.get("confidence") == "low":
            continue
        start = parse_time(ev.get("start"))
        if start and start < now - timedelta(hours=3) and not ev.get("is_live_now"):
            continue  # 지난 일정
        key = event_key(watch["id"], ev)
        if key in watch["events"]:
            continue
        watch["events"][key] = {
            "title": ev["title"], "start": start.isoformat() if start else None,
            "url": ev.get("url"), "found_at": now.isoformat(), "reminded": False,
        }
        head = "🔴 **지금 진행 중!**" if ev.get("is_live_now") else "📅 **새 일정 발견!**"
        msg = (f"{head} [{watch['title']}]\n**{ev['title']}**\n🕒 {fmt_time(start)}\n"
               f"{ev.get('summary', '')}")
        if ev.get("url"):
            msg += f"\n🔗 {ev['url']}"
        send(msg)
        if ev.get("is_live_now"):
            watch["events"][key]["reminded"] = True


def send_reminders(watch: dict) -> None:
    now = now_kst()
    for key, ev in list(watch["events"].items()):
        start = parse_time(ev.get("start"))
        if not start:
            continue
        if start < now - timedelta(days=1):
            del watch["events"][key]  # 오래된 일정 정리
            continue
        before = timedelta(minutes=watch["remind_before_minutes"])
        if not ev["reminded"] and start - before <= now <= start + timedelta(minutes=30):
            ev["reminded"] = True
            mins = int((start - now).total_seconds() // 60)
            when = f"{mins}분 뒤 시작" if mins > 0 else "지금 시작!"
            msg = f"⏰ **{when}** [{watch['title']}]\n**{ev['title']}** ({fmt_time(start)})"
            if ev.get("url"):
                msg += f"\n🔗 {ev['url']}"
            send(msg)


COMMANDS_VERSION = 1
SLASH_COMMANDS = [
    {"name": "알림", "description": "감시 등록 또는 명령 (예: 코마 시참 알려줘)",
     "options": [{"type": 3, "name": "내용", "description": "무엇을 알려드릴까요?", "required": True}]},
    {"name": "목록", "description": "등록된 감시 목록 보기"},
    {"name": "삭제", "description": "감시 삭제",
     "options": [{"type": 4, "name": "번호", "description": "삭제할 감시 번호", "required": True}]},
    {"name": "확인", "description": "감시를 지금 바로 다시 검색",
     "options": [{"type": 4, "name": "번호", "description": "감시 번호", "required": True}]},
]


def register_commands(state: dict) -> None:
    """슬래시 명령어(/알림 /목록 /삭제 /확인)를 서버에 등록 (한 번만)"""
    if state.get("commands_version") == COMMANDS_VERSION:
        return
    try:
        app = requests.get(f"{DISCORD_API}/oauth2/applications/@me", headers=discord_headers(), timeout=30)
        app.raise_for_status()
        ch = requests.get(f"{DISCORD_API}/channels/{CHANNEL_ID}", headers=discord_headers(), timeout=30)
        ch.raise_for_status()
        r = requests.put(f"{DISCORD_API}/applications/{app.json()['id']}/guilds/{ch.json()['guild_id']}/commands",
                         headers=discord_headers(), json=SLASH_COMMANDS, timeout=30)
        r.raise_for_status()
        state["commands_version"] = COMMANDS_VERSION
        print("슬래시 명령어 등록 완료")
    except Exception as e:
        print("슬래시 명령어 등록 실패:", e, file=sys.stderr)
        if not state.get("_warned_commands"):
            state["_warned_commands"] = True
            send(f"⚠️ 슬래시 명령어 등록 실패 (일반 메시지는 계속 쓸 수 있어요)\n```{str(e)[:300]}```")


def main():
    missing = [n for n, v in (("GEMINI_API_KEY 또는 ANTHROPIC_API_KEY", GEMINI_API_KEY or ANTHROPIC_API_KEY),
                              ("DISCORD_BOT_TOKEN", DISCORD_TOKEN),
                              ("DISCORD_CHANNEL_ID", CHANNEL_ID)) if not v]
    if missing:
        sys.exit("환경변수가 없어요: " + ", ".join(missing))

    state = load_state()
    first_run = state["last_message_id"] is None
    if os.environ.get("SLASH_COMMANDS") == "1":  # Cloudflare 연결 시에만 사용
        register_commands(state)

    # 1) 새 메시지 처리
    msgs = fetch_new_messages(state["last_message_id"])
    for msg in msgs:
        state["last_message_id"] = msg["id"]
        if msg.get("author", {}).get("bot"):
            # 슬래시 명령어(/알림)로 접수된 요청은 봇 메시지로 들어옴
            m = re.match(r"📨 \*\*접수:\*\* `(.+?)`", msg.get("content", ""))
            if msg.get("type") == 20 and m:
                handle_message(state, {**msg, "content": m.group(1)})
            continue
        if first_run and msg is not msgs[-1]:
            continue  # 첫 실행 땐 예전 메시지 무시하고 마지막 것만
        handle_message(state, msg)
    if first_run and not msgs:
        send(HELP_TEXT)
        state["last_message_id"] = "0"

    # 2) 감시 실행 + 리마인드
    for watch in state["watches"]:
        if watch.get("type") == "youtube":
            run_youtube(watch)
        else:
            run_watch(watch)
        send_reminders(watch)

    save_state(state)


if __name__ == "__main__":
    main()
