#!/usr/bin/env python3
"""아마란스 그룹웨어 일정을 vdir(.ics 파일)로 동기화한다. noctalia 플러그인 서비스가 주기적으로 실행한다.

Python 3.8+ 표준 라이브러리만 쓴다(키링 조회에만 libsecret의 secret-tool).
stdout 마지막 줄은 결과 JSON, 종료 코드는 0=성공 / 1=다음 주기에 재시도 / 2=사람이 설정·비밀번호를 고쳐야 함.
"""

import base64
import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Tuple

# ── 날짜 ─────────────────────────────────────────────────────────────────────


def add_days(ymd: str, n: int) -> Optional[str]:
    """`YYYYMMDD` ± n일. 형식이 틀리면 None."""
    if len(ymd) != 8 or not ymd.isdigit():
        return None
    try:
        return (datetime.strptime(ymd, "%Y%m%d") + timedelta(days=n)).strftime("%Y%m%d")
    except ValueError:
        return None


def server_to_utc(ymdhm: str, offset_hours: int) -> Optional[str]:
    """서버 시각 `YYYYMMDDHHmm` → `YYYYMMDDTHHMM00Z`."""
    if len(ymdhm) != 12 or not ymdhm.isdigit():
        return None
    try:
        t = datetime.strptime(ymdhm, "%Y%m%d%H%M") - timedelta(hours=offset_hours)
    except ValueError:
        return None
    return t.strftime("%Y%m%dT%H%M00Z")


def today(offset_hours: int) -> str:
    return datetime.now(timezone(timedelta(hours=offset_hours))).strftime("%Y%m%d")


# ── 서명 (로그인 전: get_token + SHA256, 로그인 후: wehago-sign HMAC-SHA256) ──


def transaction_id() -> str:
    return secrets.token_hex(16)


def wehago_sign(auth_token: str, tid: str, timestamp: str, path: str, sign_key: str) -> str:
    msg = (auth_token + tid + timestamp + path).encode()
    return base64.b64encode(hmac.new(sign_key.encode(), msg, hashlib.sha256).digest()).decode()


def pre_login_signature(token: str, cur_date: str, tid: str, path: str) -> str:
    return base64.b64encode(hashlib.sha256((token + cur_date + tid + path).encode()).digest()).decode()


# ── 오류 ─────────────────────────────────────────────────────────────────────


class ApiError(Exception):
    pass


class Unauthorized(ApiError):
    """HTTP 401 — 토큰 만료·훼손·서명 불일치."""


class NetworkError(ApiError):
    """연결 실패·타임아웃·401 외 HTTP 오류 — 다음 주기에 다시 시도할 일시적 실패."""


class Failed(ApiError):
    """서버가 resultCode로 거부했거나 응답 형식이 예상과 다름."""


class AuthRejected(Exception):
    """사람이 손봐야 하는 로그인 실패(비밀번호 오류, 키링 비어 있음, 직접 로그인 미지원 등)."""


class AuthTransient(Exception):
    """다음 주기에 다시 시도할 로그인 실패(네트워크 등)."""


class CookieError(Exception):
    pass


# ── 아마란스 데이터 ──────────────────────────────────────────────────────────


def is_safe_id(s: str) -> bool:
    """서버가 준 ID를 파일 경로에 써도 되는지. 경로 문자(`/`, `.`)는 거부한다."""
    return bool(re.fullmatch(r"[A-Za-z0-9_-]+", s))


def str_field(v: dict, key: str) -> str:
    """문자열 필드. 서버가 숫자로 줘도(예: `"schSeq": 10001`) 문자열로 받는다."""
    x = v.get(key) if isinstance(v, dict) else None
    if isinstance(x, str):
        return x
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        return str(x)
    return ""


@dataclass
class Calendar:
    mcal_seq: str
    cal_type: str
    title: str
    color: str

    @classmethod
    def from_json(cls, v: dict) -> Optional["Calendar"]:
        mcal_seq = str_field(v, "mcalSeq")
        if not is_safe_id(mcal_seq):
            return None
        return cls(mcal_seq, str_field(v, "calType"), str_field(v, "calTitle"), str_field(v, "calColor"))


def cal_list(cals: List[Calendar]) -> list:
    """`sc111A03`의 calList. 빈 calType은 "E"로 보정해야 그 캘린더 일정이 빠지지 않는다."""
    return [{"mcalSeq": c.mcal_seq, "calType": c.cal_type or "E", "adminYn": "Y", "color": c.color} for c in cals]


@dataclass
class Event:
    sch_seq: str
    mcal_seq: str
    title: str
    start: str
    end: str
    all_day: bool
    place: str
    contents: str
    participants: str
    modified: str

    @classmethod
    def from_json(cls, v: dict) -> Optional["Event"]:
        sch_seq, mcal_seq, start = str_field(v, "schSeq"), str_field(v, "mcalSeq"), str_field(v, "startDate")
        if not (is_safe_id(sch_seq) and is_safe_id(mcal_seq) and len(start) == 12 and start.isdigit()):
            return None
        return cls(sch_seq=sch_seq, mcal_seq=mcal_seq, title=str_field(v, "schTitle"), start=start,
                   end=str_field(v, "endDate"), all_day=str_field(v, "alldayYn") == "Y",
                   place=str_field(v, "schPlace"), contents=str_field(v, "contents"),
                   participants=str_field(v, "schUserList"), modified=str_field(v, "modifyDate"))


@dataclass
class ServerConfig:
    group_seq: str
    direct_login_supported: bool

    @classmethod
    def from_json(cls, data) -> Optional["ServerConfig"]:
        if not isinstance(data, dict) or not isinstance(data.get("groupMap"), dict):
            return None
        enc, saml = str_field(data, "credentialEncryptUseYn") == "Y", str_field(data, "samlOnlyYn") == "Y"
        return cls(str_field(data["groupMap"], "groupSeq"), not enc and not saml)


def company_info_from(uc: dict, group_seq: str) -> Optional[dict]:
    """`ucUserInfo` → companyInfo. groupSeq는 authToken에서 온 값을 쓴다.
    네 값 중 하나라도 비면 None — 빈 companyInfo로 조회하면 서버가 빈 목록을 돌려줄 수 있다."""
    comp, dept, addr, domain = (str_field(uc, k) for k in ("compSeq", "deptSeq", "emailAdd", "emailDomain"))
    if not all((comp, dept, addr, domain)):
        return None
    return {"compSeq": comp, "groupSeq": group_seq, "deptSeq": dept, "emailAddr": addr, "emailDomain": domain}


def events_body(company: dict, cals: List[Calendar], start_ymd: str, end_ymd: str, only_mine: bool) -> dict:
    return {"companyInfo": company, "startDate": start_ymd, "endDate": end_ymd, "mySchYn": "Y" if only_mine else "N",
            "calList": cal_list(cals), "tcalList": [], "acalList": [], "searchEmpSeq": "", "excelYn": None,
            "abUID": "", "sortDate": "Y", "langCode": "kr"}


def parse_calendars(items: list) -> List[Calendar]:
    """쓸 수 있는 캘린더가 하나도 없으면 오류 — 개인 캘린더는 항상 있으므로 형식 변경으로 본다.
    (빈 목록을 성공으로 넘기면 vdir 정리가 달력을 비워 버린다.)"""
    cals = [c for c in map(Calendar.from_json, items) if c]
    if not cals:
        raise Failed(f"응답 형식 변경: sc111A02 캘린더 {len(items)}개 중 해석 가능한 것이 없음")
    return cals


def parse_events(items: list) -> List[Event]:
    """항목이 있는데 하나도 해석하지 못하면 오류. 일부만 버려지면 그 수를 stderr에 남긴다."""
    events = [e for e in map(Event.from_json, items) if e]
    if items and not events:
        raise Failed(f"응답 형식 변경: sc111A03 일정 {len(items)}건 중 해석 가능한 것이 없음")
    if len(events) < len(items):
        print(f"해석할 수 없는 일정 {len(items) - len(events)}건을 건너뜀", file=sys.stderr)
    return events


# ── HTTP ─────────────────────────────────────────────────────────────────────


def normalize_server(value: str) -> Tuple[str, str]:
    """사용자가 입력한 서버 주소 → (`scheme://host[:port]`, `host`)."""
    s = value.strip()
    scheme, rest = s.split("://", 1) if "://" in s else ("https", s)
    scheme = scheme.lower()
    authority = re.split(r"[/#?]", rest, maxsplit=1)[0].lower()
    if not authority or scheme not in ("https", "http"):
        raise ValueError(f"server_url 형식이 올바르지 않습니다: '{value}' (예: https://gw.example.com)")
    return f"{scheme}://{authority}", authority.split(":")[0]


def form_urlencode(params) -> str:
    q = lambda s: urllib.parse.quote(s, safe="")  # noqa: E731
    return "&".join(f"{q(k)}={q(v)}" for k, v in params)


@dataclass
class Creds:
    auth_token: str
    sign_key: str

    @property
    def group_seq(self) -> str:
        """authToken 형식은 `groupSeq|empSeq|…`."""
        return self.auth_token.split("|")[0]


def result_data(envelope: dict):
    """표준 응답 봉투에서 resultData를 꺼낸다. resultCode는 0 또는 200이 성공이다."""
    code = envelope.get("resultCode")
    try:
        code = int(code)
    except (TypeError, ValueError):
        code = -1
    if code in (0, 200):
        return envelope.get("resultData")
    raise Failed(f"resultCode={code} resultMsg={envelope.get('resultMsg') or ''}")


class Gw:
    def __init__(self, server: str):
        self.base, self.host = normalize_server(server)

    def _request(self, method: str, path: str, headers: dict, body: Optional[bytes] = None) -> Tuple[int, bytes]:
        req = urllib.request.Request(self.base + path, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except OSError as e:  # URLError, 타임아웃, 연결 거부, TLS 오류
            raise NetworkError(f"{path.split('?')[0]} 요청 실패: {e}") from None

    def _send(self, path: str, headers: dict, body: bytes, content_type: str):
        status, raw = self._request("POST", path, {**headers, "Content-Type": content_type}, body)
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        msg = data.get("resultMsg") or "" if isinstance(data, dict) else ""
        if status == 401:
            raise Unauthorized(f"{path}: {msg}")
        if 200 <= status < 300:
            if isinstance(data, dict):
                return result_data(data)
            raise Failed(f"{path}: JSON이 아닌 응답")
        raise NetworkError(f"{path}: http={status} {msg}")

    @staticmethod
    def _signed_headers(c: Creds, path: str) -> dict:
        tid, ts = transaction_id(), str(int(time.time()))
        return {"Authorization": f"Bearer {c.auth_token}", "timestamp": ts, "transaction-id": tid,
                "wehago-sign": wehago_sign(c.auth_token, tid, ts, path, c.sign_key)}

    def _pre_login_headers(self, path: str) -> dict:
        """로그인 전 서명: get_token으로 받은 token·cur_date로 SHA256. tid는 두 요청에서 같아야 한다."""
        tid = transaction_id()
        status, raw = self._request("GET", f"/get_token/?url={path}&_={int(time.time() * 1000)}", {"transaction-id": tid})
        if not 200 <= status < 300:
            raise NetworkError(f"get_token: http={status}")
        try:
            v = json.loads(raw)
            token, cur_date = v["token"], v["cur_date"]
        except (ValueError, KeyError, TypeError):
            raise Failed("응답 형식 변경: get_token") from None
        return {"transaction-id": tid, "timestamp": str(int(time.time())),
                "signature": pre_login_signature(token, cur_date, tid, path)}

    def post_json(self, c: Creds, path: str, body: dict):
        return self._send(path, self._signed_headers(c, path), json.dumps(body, ensure_ascii=False).encode(), "application/json")

    def post_form(self, c: Creds, path: str, params):
        return self._send(path, self._signed_headers(c, path), form_urlencode(params).encode(), "application/x-www-form-urlencoded")

    def pre_login_json(self, path: str, body: dict):
        return self._send(path, self._pre_login_headers(path), json.dumps(body, ensure_ascii=False).encode(), "application/json")

    def pre_login_form(self, path: str, params):
        return self._send(path, self._pre_login_headers(path), form_urlencode(params).encode(), "application/x-www-form-urlencoded")


# ── 엔드포인트 ───────────────────────────────────────────────────────────────


def server_config(gw: Gw) -> ServerConfig:
    """로그인 전 서버 설정(gw050B01). ID/PW 로그인 직전에만 호출한다."""
    cfg = ServerConfig.from_json(gw.pre_login_json("/gw/gw050B01", {"host": gw.host}))
    if cfg is None:
        raise Failed("응답 형식 변경: gw050B01 groupMap")
    return cfg


def company_info(gw: Gw, c: Creds) -> dict:
    """로그인된 세션의 companyInfo(gw050A02, Bearer). groupSeq는 authToken에서."""
    data = gw.post_form(c, "/gw/gw050A02", [("a10Domain", gw.base)])
    uc = ((data or {}).get("sessionInfo") or {}).get("ucUserInfo") if isinstance(data, dict) else None
    info = company_info_from(uc, c.group_seq) if isinstance(uc, dict) else None
    if info is None:
        raise Failed("응답 형식 변경: gw050A02 sessionInfo.ucUserInfo")
    return info


def _result_list(data, api: str) -> list:
    items = data.get("resultList") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise Failed(f"응답 형식 변경: {api} resultList")
    return items


def calendars(gw: Gw, c: Creds, company: dict) -> List[Calendar]:
    data = gw.post_json(c, "/schres/sc111A02", {"companyInfo": company, "calType": "", "langCode": "kr"})
    return parse_calendars(_result_list(data, "sc111A02"))


def events(gw: Gw, c: Creds, company: dict, cals: List[Calendar], start: str, end: str, only_mine: bool) -> List[Event]:
    data = gw.post_json(c, "/schres/sc111A03", events_body(company, cals, start, end, only_mine))
    return parse_events(_result_list(data, "sc111A03"))


# ── ① ID/PW 로그인 (비밀번호는 secret-tool의 stdout으로만 받고, 어디에도 남기지 않는다) ──


def secret_lookup(program: str, attrs: List[str]) -> str:
    hint = "키링에 비밀번호가 없습니다 — 등록: secret-tool store --label='Amaranth' " + " ".join(attrs)
    try:
        out = subprocess.run([program, "lookup", *attrs], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=90)
    except subprocess.TimeoutExpired:
        raise AuthTransient("키링 응답 시간 초과 — 키링 잠금을 해제하세요") from None
    except OSError as e:
        raise AuthRejected(f"{program} 실행 실패({e}) — libsecret을 설치하세요. {hint}") from None
    password = out.stdout.decode(errors="replace").rstrip("\r\n")
    if out.returncode != 0 or not password:
        raise AuthRejected(hint)
    return password


def lookup_password(host: str, login_id: str) -> str:
    return secret_lookup("secret-tool", ["service", "amaranth-sync", "server", host, "account", login_id])


def parse_login(data) -> Optional[Tuple[Creds, int]]:
    s = data.get("sessionInfo") if isinstance(data, dict) else None
    token, key = str_field(s, "auth_a_token"), str_field(s, "hash_key")
    if not token or not key:
        return None
    try:
        secs = int(s.get("sessionTime"))
    except (TypeError, ValueError):
        secs = 86400
    return Creds(token, key), secs


def _api_to_auth(e: ApiError) -> Exception:
    return AuthTransient(str(e)) if isinstance(e, NetworkError) else AuthRejected(f"로그인 실패: {e}")


def login(gw: Gw, login_id: str, password: str) -> Tuple[Creds, int]:
    try:
        cfg = server_config(gw)
    except ApiError as e:
        raise _api_to_auth(e) from None
    if not cfg.direct_login_supported:
        raise AuthRejected("이 서버는 ID/PW 직접 로그인을 지원하지 않습니다 — 인증 방식을 browser로 바꾸세요")
    domain = gw.base
    # 웹 클라이언트와 같은 선호출. 결과는 쓰지 않지만 네트워크 오류는 일시적 실패로 본다.
    try:
        gw.pre_login_form("/gw/gw050A02", [("loginId", base64.b64encode(login_id.encode()).decode()),
                                           ("groupSeq", cfg.group_seq), ("loginType", "checkLoginId"),
                                           ("apiTarget", "web"), ("langCode", "kr"), ("a10Domain", domain)])
    except NetworkError as e:
        raise AuthTransient(str(e)) from None
    except ApiError:
        pass
    form = [("loginId", login_id), ("password", password), ("groupSeq", cfg.group_seq), ("scLoginYn", "N"),
            ("fidoPasswdLoginYn", "null"), ("langCode", "kr"), ("isChangeLangCodeYn", "N"), ("a10Domain", domain),
            ("isPlainText", "true"), ("isMobileVerificationIgnore", "N")]
    try:
        data = gw.pre_login_form("/gw/gw050A02", form)
    except ApiError as e:
        raise _api_to_auth(e) from None
    parsed = parse_login(data)
    if parsed is None:
        raise AuthRejected("로그인 응답에 세션 정보가 없습니다(추가 인증 요구 등) — 웹에서 로그인해 확인하세요")
    return parsed


def cache_path(host: str, login_id: str) -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", login_id)
    return base / "amaranth-sync" / f"{host}-{safe_id}.json"


def load_cache(path: Path, now: int) -> Optional[Creds]:
    try:
        v = json.loads(path.read_text())
        if int(v["expires_at"]) <= now:
            return None
        return Creds(v["auth_token"], v["sign_key"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_cache(path: Path, c: Creds, expires_at: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        os.fchmod(fd, 0o600)  # 이미 있던 파일이어도 0600으로
        json.dump({"auth_token": c.auth_token, "sign_key": c.sign_key, "expires_at": expires_at}, f)


def password_creds(gw: Gw, login_id: str, force_login: bool) -> Creds:
    path, now = cache_path(gw.host, login_id), int(time.time())
    if not force_login:
        cached = load_cache(path, now)
        if cached:
            print(f"세션 캐시 사용: {path}", file=sys.stderr)
            return cached
    password = lookup_password(gw.host, login_id)
    print(f"로그인: {gw.base} ({login_id})", file=sys.stderr)
    creds, secs = login(gw, login_id, password)
    try:
        save_cache(path, creds, now + max(0, secs - 600))
    except OSError as e:
        print(f"세션 캐시 저장 실패(다음 실행에 다시 로그인): {e}", file=sys.stderr)
    return creds


# ── ② 브라우저 쿠키 (Chrome·Chromium은 AES-128-CBC 암호화, Firefox는 평문) ──


@lru_cache(maxsize=1)
def _aes_tables() -> Tuple[List[int], List[int]]:
    xtime = lambda b: ((b << 1) ^ 0x1B) & 0xFF if b & 0x80 else b << 1  # noqa: E731
    exp, log, x = [0] * 255, [0] * 256, 1
    for i in range(255):  # 생성원 3의 거듭제곱으로 GF(2^8) 역원 계산
        exp[i], log[x] = x, i
        x ^= xtime(x)
    sbox = []
    for b in range(256):
        inv = exp[(255 - log[b]) % 255] if b else 0
        s = inv
        for k in range(1, 5):
            s ^= ((inv << k) | (inv >> (8 - k))) & 0xFF
        sbox.append(s ^ 0x63)
    inv_sbox = [0] * 256
    for i, s in enumerate(sbox):
        inv_sbox[s] = i
    return sbox, inv_sbox


def _gmul(a: int, b: int) -> int:
    r = 0
    while b:
        if b & 1:
            r ^= a
        a = ((a << 1) ^ 0x1B) & 0xFF if a & 0x80 else a << 1
        b >>= 1
    return r


def _round_keys(key: bytes) -> List[List[int]]:
    sbox = _aes_tables()[0]
    rcon = [1, 2, 4, 8, 16, 32, 64, 128, 0x1B, 0x36]
    w = [list(key[i:i + 4]) for i in range(0, 16, 4)]
    for i in range(4, 44):
        t = list(w[i - 1])
        if i % 4 == 0:
            t = [sbox[b] for b in t[1:] + t[:1]]
            t[0] ^= rcon[i // 4 - 1]
        w.append([x ^ y for x, y in zip(w[i - 4], t)])
    return [sum(w[4 * r:4 * r + 4], []) for r in range(11)]


def _decrypt_block(rk: List[List[int]], block: bytes) -> bytes:
    inv_sbox = _aes_tables()[1]
    s = [b ^ k for b, k in zip(block, rk[10])]
    for rnd in range(9, -1, -1):
        s = [inv_sbox[s[r + 4 * ((c - r) % 4)]] for c in range(4) for r in range(4)]  # InvShiftRows + InvSubBytes
        s = [b ^ k for b, k in zip(s, rk[rnd])]
        if rnd:  # InvMixColumns
            out = []
            for c in range(4):
                a0, a1, a2, a3 = s[4 * c:4 * c + 4]
                out += [_gmul(a0, 14) ^ _gmul(a1, 11) ^ _gmul(a2, 13) ^ _gmul(a3, 9),
                        _gmul(a0, 9) ^ _gmul(a1, 14) ^ _gmul(a2, 11) ^ _gmul(a3, 13),
                        _gmul(a0, 13) ^ _gmul(a1, 9) ^ _gmul(a2, 14) ^ _gmul(a3, 11),
                        _gmul(a0, 11) ^ _gmul(a1, 13) ^ _gmul(a2, 9) ^ _gmul(a3, 14)]
            s = out
    return bytes(s)


def aes128_decrypt_block(key: bytes, block: bytes) -> bytes:
    return _decrypt_block(_round_keys(key), block)


def derive_key(secret: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha1", secret, b"saltysalt", 1, 16)


def chrome_key(browser: str) -> bytes:
    """키링의 "Chrome Safe Storage" 비밀(v11), 없으면 고정 비밀 "peanuts"(v10)."""
    for app in (["chromium", "chrome"] if browser == "chromium" else ["chrome", "chromium"]):
        try:
            out = subprocess.run(["secret-tool", "lookup", "application", app], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=90)
        except (OSError, subprocess.TimeoutExpired):
            break
        secret = out.stdout.rstrip(b"\r\n")
        if out.returncode == 0 and secret:
            return derive_key(secret)
    return derive_key(b"peanuts")


def decrypt_chrome(enc: bytes, key: bytes) -> str:
    """`v10`/`v11` 접두 3바이트를 떼고 AES-128-CBC(iv=0x20×16)로 푼다.
    최신 Chrome은 평문 앞에 32바이트 도메인 해시를 붙이므로, UTF-8이 아니면 그만큼 버린다."""
    data = enc[3:]
    if len(enc) < 3 or not data or len(data) % 16:
        raise CookieError("암호화된 쿠키 값의 길이가 올바르지 않습니다")
    rk, prev, out = _round_keys(key), b" " * 16, bytearray()
    for i in range(0, len(data), 16):
        block = data[i:i + 16]
        out += bytes(x ^ y for x, y in zip(_decrypt_block(rk, block), prev))
        prev = block
    pad = out[-1]
    if not 1 <= pad <= 16 or out[-pad:] != bytes([pad]) * pad:
        raise CookieError("쿠키 복호화 실패 — 키링의 Chrome 키를 확인하세요")
    pt = bytes(out[:-pad])
    try:
        return pt.decode()
    except UnicodeDecodeError:
        return pt[32:].decode(errors="replace")


def pct_decode(s: str) -> str:
    return urllib.parse.unquote_to_bytes(s).decode(errors="replace")


def default_firefox_profile(profiles_ini: str) -> Optional[str]:
    """profiles.ini에서 기본 프로필 경로. `[Install…]`의 `Default=`가 우선, 없으면 `Default=1`인 프로필."""
    section, install_default, profile_default, path, is_default = "", None, None, None, False
    for line in [ln.strip() for ln in profiles_ini.splitlines()] + ["[end]"]:
        if line.startswith("["):
            if section.startswith("[Profile") and is_default and profile_default is None:
                profile_default = path
            section, path, is_default = line, None, False
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        if section.startswith("[Install"):
            if k == "Default" and install_default is None:
                install_default = v
        elif k == "Path":
            path = v
        elif k == "Default":
            is_default = v == "1"
    return install_default or profile_default


@contextlib.contextmanager
def private_copy(src: Path):
    """브라우저가 잠그고 있는 쿠키 DB(+WAL)를 0700 임시 폴더에 0600으로 복사한다. 끝나면 지운다.
    모든 사이트의 쿠키가 담긴 파일이라 다른 사용자가 읽을 수 없어야 한다."""
    tmp = Path(tempfile.mkdtemp(prefix="amaranth-sync-cookies-"))
    try:
        dst = tmp / "cookies.db"
        for s, d in [(src, dst), (Path(f"{src}-wal"), Path(f"{dst}-wal"))]:
            if s == src or s.exists():
                fd = os.open(d, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as out, open(s, "rb") as inp:
                    shutil.copyfileobj(inp, out)
        yield dst
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _read_cookies(db: Path, sql: str, host: str) -> List[Tuple[str, bytes]]:
    try:
        with private_copy(db) as copy:
            conn = sqlite3.connect(str(copy))
            try:
                return [(name, bytes(value or b"")) for name, value in conn.execute(sql, (host, "." + host))]
            finally:
                conn.close()
    except (OSError, sqlite3.Error) as e:
        raise CookieError(f"쿠키 DB를 읽을 수 없습니다({db}): {e}") from None


def browser_creds(browser: str, profile: Optional[str], host: str) -> Creds:
    home = Path.home()
    if browser == "firefox":
        root = home / ".mozilla/firefox"
        if not profile:
            try:
                profile = default_firefox_profile((root / "profiles.ini").read_text())
            except OSError as e:
                raise CookieError(f"Firefox 프로필을 찾을 수 없습니다({root}): {e}") from None
            if not profile:
                raise CookieError("profiles.ini에 기본 프로필이 없습니다 — browser_profile을 지정하세요")
        rows = _read_cookies(root / profile / "cookies.sqlite",
                             "SELECT name, CAST(value AS BLOB) FROM moz_cookies WHERE host IN (?, ?)", host)
        get = lambda n: next((v.decode(errors="replace") for k, v in rows if k == n), None)  # noqa: E731
    else:
        base = home / ".config" / ("google-chrome" if browser == "chrome" else "chromium") / (profile or "Default")
        db = next((p for p in (base / "Network/Cookies", base / "Cookies") if p.exists()), None)
        if db is None:
            raise CookieError(f"쿠키 DB가 없습니다({base}) — browser·browser_profile 설정을 확인하세요")
        rows = _read_cookies(db, "SELECT name, encrypted_value FROM cookies WHERE host_key IN (?, ?)", host)
        key = chrome_key(browser)
        get = lambda n: next((decrypt_chrome(v, key) for k, v in rows if k == n), None)  # noqa: E731
    at, hk = get("BIZCUBE_AT"), get("BIZCUBE_HK")
    if not at or not hk:
        raise CookieError(f"브라우저에 로그인 쿠키가 없습니다 — 브라우저에서 https://{host} 에 다시 로그인하세요")
    return Creds(pct_decode(at), pct_decode(hk))


# ── ICS ──────────────────────────────────────────────────────────────────────


def file_name(ev: Event) -> str:
    return f"{ev.start[:8]}-{ev.sch_seq}.ics"


def plain_text(s: str) -> str:
    """HTML일 수 있는 contents를 평문으로. 평문 입력은 앞뒤 공백만 잘린다."""
    out, rest = [], s
    while "<" in rest:
        open_ = rest.index("<")
        out.append(rest[:open_])
        close = rest.find(">", open_)
        if close < 0:
            rest = rest[open_:]
            break
        tag = rest[open_ + 1:close].strip().lower()
        if tag == "/p" or tag.rstrip("/").strip() == "br":
            out.append("\n")
        rest = rest[close + 1:]
    text = "".join(out) + rest
    for src, dst in [("&nbsp;", " "), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'"), ("&amp;", "&")]:
        text = text.replace(src, dst)
    return text.strip()


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r", "").replace("\n", "\\n")


def _fold(line: str) -> str:
    """RFC 5545 줄 접기: 한 줄 75옥텟 이하, 이어지는 줄은 공백 1개로 시작. 문자 중간에서 자르지 않는다."""
    out, n = [], 0
    for ch in line:
        w = len(ch.encode())
        if n + w > 75:
            out.append("\r\n ")
            n = 1
        out.append(ch)
        n += w
    return "".join(out)


def to_ics(ev: Event, host: str, utc_offset: int) -> str:
    start_utc = server_to_utc(ev.start, utc_offset) or ""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//ykoh//amaranth-sync//KO", "BEGIN:VEVENT",
             f"UID:amaranth-{ev.sch_seq}@{host}", f"DTSTAMP:{server_to_utc(ev.modified, utc_offset) or start_utc}"]
    if ev.all_day:
        start_day = ev.start[:8]
        end_day = ev.end[:8] if len(ev.end) >= 8 and ev.end[:8].isdigit() else start_day
        lines.append(f"DTSTART;VALUE=DATE:{start_day}")
        lines.append(f"DTEND;VALUE=DATE:{add_days(max(end_day, start_day), 1) or start_day}")
    else:
        end_utc = (server_to_utc(ev.end, utc_offset) if ev.end >= ev.start else None) or start_utc
        lines += [f"DTSTART:{start_utc}", f"DTEND:{end_utc}"]
    lines.append(f"SUMMARY:{_escape(ev.title)}")
    if ev.place:
        lines.append(f"LOCATION:{_escape(ev.place)}")
    desc = plain_text(ev.contents)
    if ev.participants:
        desc += ("\n" if desc else "") + f"참여: {ev.participants}"
    if desc:
        lines.append(f"DESCRIPTION:{_escape(desc)}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "".join(_fold(line) + "\r\n" for line in lines)


# ── vdir: `{root}/{mcalSeq}/` 하나가 noctalia의 캘린더 컬렉션 하나 ─────────────

STATE_FILE = ".amaranth-sync.json"
OWN_FILE = re.compile(r"\d{8}-[A-Za-z0-9_-]+\.ics")


@dataclass
class Entry:
    mcal_seq: str
    file_name: str
    content: str


@dataclass
class Stats:
    written: int = 0
    deleted: int = 0
    unchanged: int = 0


def _write_if_changed(path: Path, content: str) -> bool:
    """내용이 다를 때만 임시 파일 + rename으로 교체한다(불필요한 inotify 재로딩 방지). 바꿨으면 True."""
    data = content.encode()
    try:
        if path.read_bytes() == data:
            return False
    except OSError:
        pass
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return True


def _own_files(root: Path, collections) -> List[Tuple[str, str, Path]]:
    """우리 컬렉션 디렉터리들 안의 우리 `.ics` 파일(이름이 `YYYYMMDD-{id}.ics`). 다른 도구의 파일은 절대 건드리지 않는다."""
    out = []
    for d in sorted(collections):
        try:
            names = os.listdir(root / d)
        except OSError:
            continue
        out += [(d, n, root / d / n) for n in names if OWN_FILE.fullmatch(n) and (root / d / n).is_file()]
    return out


def apply_vdir(root: Path, state: dict, cals: List[Calendar], entries: List[Entry], window: Tuple[str, str]) -> Stats:
    stats = Stats()
    root.mkdir(parents=True, exist_ok=True)

    # 상태 파일: 마지막 동기화의 {서버, 토글}과, 지금까지 이 프로그램이 만든 컬렉션 목록.
    state_path = root / STATE_FILE
    try:
        previous = json.loads(state_path.read_text())
    except (OSError, ValueError):
        previous = None
    prev_cols = previous.get("collections") if isinstance(previous, dict) else None
    collections = {c for c in (prev_cols if isinstance(prev_cols, list) else []) if isinstance(c, str) and is_safe_id(c)}

    # 서버나 "내 일정만" 토글이 바뀌면 이전 모드의 일정(범위 밖 포함)이 섞이지 않게 우리 파일을 전부 지운다.
    if isinstance(previous, dict) and previous.get("state") != state:
        for _, _, path in _own_files(root, collections):
            path.unlink()
            stats.deleted += 1
    collections |= {c.mcal_seq for c in cals} | {e.mcal_seq for e in entries}

    for c in cals:
        d = root / c.mcal_seq
        d.mkdir(exist_ok=True)
        _write_if_changed(d / "displayname", c.title)
        if re.fullmatch(r"#[0-9A-Fa-f]{6}", c.color):
            _write_if_changed(d / "color", c.color)

    keep = set()
    for e in entries:
        d = root / e.mcal_seq
        d.mkdir(exist_ok=True)
        if _write_if_changed(d / e.file_name, e.content):
            stats.written += 1
        else:
            stats.unchanged += 1
        keep.add((e.mcal_seq, e.file_name))

    # 조회 범위 안에서 시작하는데 이번 결과에 없는 일정만 지운다. 범위 밖 지난 일정은 남긴다.
    for d, name, path in _own_files(root, collections):
        if window[0] <= name[:8] <= window[1] and (d, name) not in keep:
            path.unlink()
            stats.deleted += 1

    new_state = {"state": state, "collections": sorted(collections)}
    _write_if_changed(state_path, json.dumps(new_state, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    return stats


# ── CLI ──────────────────────────────────────────────────────────────────────

USAGE = ("usage: amaranth_sync.py --server URL --vdir PATH [--only-mine|--all] [--past-days N] [--future-days N] "
         "[--utc-offset N] (--auth password --login-id ID | --auth browser --browser chrome|chromium|firefox [--profile P])")


class UsageError(Exception):
    pass


class SyncTransient(Exception):
    """종료 코드 1: 다음 주기에 재시도."""


class SyncRejected(Exception):
    """종료 코드 2: 사람이 설정·비밀번호를 고쳐야 함(서비스가 자동 동기화를 멈춘다)."""


@dataclass
class PasswordAuth:
    login_id: str


@dataclass
class BrowserAuth:
    browser: str
    profile: Optional[str]


@dataclass
class Args:
    server: str
    vdir: Path
    only_mine: bool
    past_days: int
    future_days: int
    utc_offset: int
    auth: object


def _expand_home(p: str) -> Path:
    if p == "~":
        return Path.home()
    return Path.home() / p[2:] if p.startswith("~/") else Path(p)


def parse_args(argv: List[str]) -> Args:
    opts = {"only_mine": True, "past_days": 14, "future_days": 60, "utc_offset": 9}
    it = iter(argv)
    for flag in it:
        if flag in ("--only-mine", "--all"):
            opts["only_mine"] = flag == "--only-mine"
            continue
        key = {"--server": "server", "--vdir": "vdir", "--auth": "auth", "--login-id": "login_id", "--browser": "browser",
               "--profile": "profile", "--past-days": "past_days", "--future-days": "future_days",
               "--utc-offset": "utc_offset"}.get(flag)
        if key is None:
            raise UsageError(f"알 수 없는 인자: {flag}")
        value = next(it, None)
        if value is None:
            raise UsageError(f"{flag}에 값이 필요합니다")
        if key in ("past_days", "future_days", "utc_offset"):
            try:
                opts[key] = int(value)
            except ValueError:
                raise UsageError(f"{flag}는 정수여야 합니다: {value}") from None
        else:
            opts[key] = value

    def required(key: str, msg: str) -> str:
        v = (opts.get(key) or "").strip()
        if not v:
            raise UsageError(msg)
        return v

    auth = required("auth", "--auth가 필요합니다")
    if auth == "password":
        auth_args = PasswordAuth(required("login_id", "--auth password에는 --login-id(설정의 로그인 ID)가 필요합니다"))
    elif auth == "browser":
        browser = required("browser", "--auth browser에는 --browser가 필요합니다")
        if browser not in ("chrome", "chromium", "firefox"):
            raise UsageError("--browser는 chrome, chromium, firefox 중 하나여야 합니다")
        auth_args = BrowserAuth(browser, (opts.get("profile") or "").strip() or None)
    else:
        raise UsageError(f"--auth는 password 또는 browser여야 합니다: {auth}")
    return Args(server=required("server", "--server가 필요합니다"), vdir=_expand_home(required("vdir", "--vdir가 필요합니다")),
                only_mine=opts["only_mine"], past_days=opts["past_days"], future_days=opts["future_days"],
                utc_offset=opts["utc_offset"], auth=auth_args)


def still_unauthorized(auth, base: str, msg: str) -> Exception:
    """인증 정보를 새로 확보했는데도 401일 때.
    - browser: 사용자가 브라우저에서 다시 로그인하면 풀리므로 재시도(종료 1).
    - password: 방금 받은 토큰이 거부된 것이라 다음 주기에도 로그인만 반복된다. 사람이 확인할 때까지 멈춘다(종료 2)."""
    if isinstance(auth, BrowserAuth):
        return SyncTransient(f"브라우저에서 {base} 에 다시 로그인하세요")
    return SyncRejected(f"로그인은 됐지만 서버가 새 토큰을 거부했습니다 — 자동 동기화를 멈춥니다: {msg}")


def _acquire(gw: Gw, auth, refresh: bool) -> Creds:
    try:
        if isinstance(auth, PasswordAuth):
            return password_creds(gw, auth.login_id, refresh)
        return browser_creds(auth.browser, auth.profile, gw.host)
    except AuthRejected as e:
        raise SyncRejected(str(e)) from None
    except (AuthTransient, CookieError) as e:
        raise SyncTransient(str(e)) from None


def _fetch(gw: Gw, c: Creds, start: str, end: str, only_mine: bool):
    company = company_info(gw, c)
    cals = calendars(gw, c, company)
    return cals, events(gw, c, company, cals, start, end, only_mine)


def run(args: Args) -> Tuple[int, int]:
    try:
        gw = Gw(args.server)
    except ValueError as e:
        raise SyncRejected(str(e)) from None
    day = today(args.utc_offset)
    start, end = add_days(day, -args.past_days), add_days(day, args.future_days)
    try:
        try:
            cals, evs = _fetch(gw, _acquire(gw, args.auth, False), start, end, args.only_mine)
        except Unauthorized as e:
            print(f"401 — 인증 정보를 다시 확보해 1회 재시도합니다: {e}", file=sys.stderr)
            try:
                cals, evs = _fetch(gw, _acquire(gw, args.auth, True), start, end, args.only_mine)
            except Unauthorized as e2:
                raise still_unauthorized(args.auth, gw.base, str(e2)) from None
    except ApiError as e:
        raise SyncTransient(str(e)) from None

    entries = [Entry(e.mcal_seq, file_name(e), to_ics(e, gw.host, args.utc_offset)) for e in evs]
    try:
        stats = apply_vdir(args.vdir, {"server": gw.base, "only_mine": args.only_mine}, cals, entries, (start, end))
    except OSError as e:
        raise SyncTransient(f"vdir 기록 실패({args.vdir}): {e}") from None
    print(f"동기화 완료 {start}~{end}: 일정 {len(entries)}건(새로 씀 {stats.written}, 그대로 {stats.unchanged}, "
          f"삭제 {stats.deleted}), 캘린더 {len(cals)}개", file=sys.stderr)
    return len(entries), len(cals)


def main(argv: List[str]) -> int:
    try:
        n_events, n_cals = run(parse_args(argv))
        result, code = {"ok": True, "events": n_events, "calendars": n_cals}, 0
    except UsageError as e:
        result, code = {"ok": False, "error": f"{e}\n{USAGE}"}, 2
    except SyncRejected as e:
        result, code = {"ok": False, "error": str(e)}, 2
    except SyncTransient as e:
        result, code = {"ok": False, "error": str(e)}, 1
    except Exception as e:  # 예상하지 못한 오류도 결과 JSON으로 남겨 서비스가 알릴 수 있게 한다
        traceback.print_exc()
        result, code = {"ok": False, "error": f"내부 오류: {e}"}, 1
    if not result["ok"]:
        print(f"오류: {result['error']}", file=sys.stderr)
    print(json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
