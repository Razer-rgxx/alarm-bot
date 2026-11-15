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
DISCORD_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID", "")

DISCORD_API = "https://discord.com/api/v10"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

HELP_TEXT = (
    "**🔔 Gemini 알림봇 사용법**\n"
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
def gemini(prompt: str, search: bool = False, json_mode: bool = False) -> str:
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
  "understood": "사용자에게 보여줄 한 줄 확인 문장 (예: 코마님이 시참을 예고하면 바로, 시작 30분 전에 한 번 더 알려드릴게요)"
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


def search_events(watch: dict) -> list:
    text = gemini(SEARCH_PROMPT.format(now=now_kst().isoformat(timespec="minutes"), **{
        k: watch.get(k, "") for k in ("title", "event_description", "where_to_look", "search_query")
    }), search=True)
    data = extract_json(text)
    events = data.get("events", []) if isinstance(data, dict) else data
    return [e for e in events if isinstance(e, dict) and e.get("title")]


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
            lines.append(f"`{w['id']}` **{w['title']}** — {w['check_every_hours']}시간마다 확인, "
                         f"알고 있는 일정 {len(upcoming)}개")
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
    try:
        plan = make_plan(text)
    except Exception as e:
        send(f"⚠️ 요청을 이해하는 중 오류가 났어요: {e}", mid)
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


def main():
    missing = [n for n, v in (("GEMINI_API_KEY", GEMINI_API_KEY), ("DISCORD_BOT_TOKEN", DISCORD_TOKEN),
                              ("DISCORD_CHANNEL_ID", CHANNEL_ID)) if not v]
    if missing:
        sys.exit("환경변수가 없어요: " + ", ".join(missing))

    state = load_state()
    first_run = state["last_message_id"] is None

    # 1) 새 메시지 처리
    msgs = fetch_new_messages(state["last_message_id"])
    for msg in msgs:
        state["last_message_id"] = msg["id"]
        if msg.get("author", {}).get("bot"):
            continue
        if first_run and msg is not msgs[-1]:
            continue  # 첫 실행 땐 예전 메시지 무시하고 마지막 것만
        handle_message(state, msg)
    if first_run and not msgs:
        send(HELP_TEXT)
        state["last_message_id"] = "0"

    # 2) 감시 실행 + 리마인드
    for watch in state["watches"]:
        run_watch(watch)
        send_reminders(watch)

    save_state(state)


if __name__ == "__main__":
    main()
