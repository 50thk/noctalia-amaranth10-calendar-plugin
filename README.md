# Amaranth Calendar for noctalia

더존 **아마란스** 그룹웨어의 일정을 [noctalia](https://docs.noctalia.dev/) 내장 캘린더에 표시하는 플러그인입니다.

- 플러그인 서비스가 주기적으로 `amaranth_sync.py`(Python 표준 라이브러리만 사용)를 실행합니다.
- 이 스크립트가 아마란스 REST API로 일정을 받아 `.ics` 파일(vdir)로 씁니다.
- noctalia 캘린더가 vdir 계정으로 그 파일을 읽어, 달력에 표시하고 일정 알림(기본 10분 전)을 띄웁니다.

일정은 **조회 전용**입니다. 회사별 값은 서버 주소만 넣으면 되고, groupSeq·조직 정보·캘린더 목록은 자동으로 알아냅니다. 따로 빌드하거나 설치할 것은 없고, `python3`와 `libsecret`(`secret-tool`)만 있으면 됩니다.

## 설치

### 1. 플러그인 추가

```sh
noctalia msg plugins source add amaranth git https://github.com/50thk/noctalia-amaranth10-calendar-plugin.git
noctalia msg plugins enable ykoh/amaranth-calendar
```

처음 켜면 플러그인 설정 화면이 열립니다. **아마란스 서버 주소**와 **인증 방식**을 지정하세요.

| 설정 | 기본값 | 설명 |
|---|---|---|
| 아마란스 서버 주소 | (없음) | 예: `https://gw.example.com` |
| 인증 방식 | ID/PW | ID/PW(키링) 또는 브라우저 쿠키 |
| 로그인 ID / 브라우저 / 브라우저 프로필 | | 인증 방식에 따라 표시 |
| 내 일정만 보기 | 켬 | 끄면 볼 수 있는 캘린더의 전체 일정 |
| 동기화 주기(분) | 15 | |
| 조회 범위: 과거·미래(일) | 14 · 60 | |
| 서버 시간대(UTC+N) | 9 | 고급 |
| 일정 저장 경로(vdir) | `~/.local/share/calendars/amaranth` | 고급. 이 플러그인 전용 폴더 |

### 2. 인증 방식 준비

**ID/PW(기본)**: 비밀번호를 OS 키링(gnome-keyring, KDE Wallet)에 한 번 등록합니다.

> ⚠️ ID/PW 방식은 아직 실서버에서 끝까지 검증되지 않았습니다. 실서버 검증은 브라우저 쿠키 방식으로 마쳤습니다.

```sh
secret-tool store --label='Amaranth (gw.example.com)' service amaranth-sync server gw.example.com account <로그인ID>
```

- 로그인은 세션당 한 번(실측한 서버 기준 약 24시간)만 합니다.
- 세션 토큰은 `~/.local/state/amaranth-sync/`에 권한 0600으로 캐시됩니다.
- 자격증명 암호화를 쓰거나 SAML 전용인 서버는 ID/PW 직접 로그인을 지원하지 않습니다. 이때는 브라우저 쿠키 방식을 쓰세요.

**브라우저 쿠키**: Chrome, Chromium, Firefox 중 하나로 아마란스에 로그인해 두기만 하면 됩니다. 쿠키가 만료되면 브라우저에서 다시 로그인하세요.

### 3. noctalia 캘린더에 vdir 계정 추가

플러그인은 noctalia 설정을 수정할 수 없습니다. 설정이 빠져 있으면 플러그인이 알림으로 알려 줍니다.

설정 화면의 **캘린더**에서 추가하거나, `~/.config/noctalia/config.toml`에 직접 넣으세요. noctalia 캘린더는 **기본값이 꺼짐**이라 `enabled = true`가 필요하고, `path`는 플러그인의 "일정 저장 경로"와 같아야 합니다.

```toml
[calendar]
enabled = true

[calendar.account.amaranth]
type = "vdir"
name = "아마란스"
path = "~/.local/share/calendars/amaranth"
```

아마란스 캘린더마다 하위 디렉터리가 하나씩 생겨, noctalia에서 캘린더별 색으로 구분됩니다.

## 사용

- 자동 동기화: 설정한 주기마다 실행되고, 설정을 바꾸면 곧바로 다시 동기화합니다.
- 수동 동기화: `noctalia msg plugin ykoh/amaranth-calendar:sync all sync`
- 일정 알림: noctalia `[calendar.reminders]`가 처리합니다(기본 시작 10분 전, 종일 일정은 매일 09:00 요약).
- 업데이트: `noctalia msg plugins update amaranth` 뒤에는 실행 중인 서비스가 이전 코드를 계속 쓰므로, `noctalia msg plugins disable ykoh/amaranth-calendar` → `enable`로 다시 켜야 새 코드가 적용됩니다.

| 종료 코드 | 의미 | 플러그인 동작 |
|---|---|---|
| 0 | 성공 | — |
| 1 | 일시적 실패(네트워크, 서버 오류, 브라우저 세션 만료) | 첫 실패만 알림, 다음 주기에 재시도 |
| 2 | 사람이 고쳐야 하는 실패(비밀번호 오류·미등록, 직접 로그인 미지원 서버, 잘못된 설정) | 알림 후 자동 동기화 중단. 설정을 바꾸거나 수동 동기화하면 재개 |

ID/PW 방식은 로그인에 실패하면 다시 시도하지 않습니다. 그래서 계정이 잠기거나 인증 푸시가 반복되지 않습니다.

## 개발

```sh
python3 -m unittest discover -s tests                                         # 단위 테스트
AMARANTH_TEST_SERVER=gw.example.com python3 -m unittest discover -s tests -k live   # 실서버·실브라우저 테스트(이 PC의 Chrome 로그인 세션 필요)
noctalia plugins lint amaranth-calendar                                       # 플러그인 정적 검사
python3 amaranth-calendar/amaranth_sync.py --server gw.example.com --vdir /tmp/v --auth browser --browser chrome   # 직접 실행
```

API 서명과 쿠키 복호화는 [inno-creed](https://github.com/zilhak/inno-creed)를 참고했습니다.
