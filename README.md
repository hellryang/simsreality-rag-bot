# AI 기반 업무협업 플랫폼 연동 및 자동화 서비스

㈜심스리얼리티 산학협력 프로젝트 (2026.07.27 ~ 09.18)

Notion·Slack·KakaoWork 세 곳에 흩어진 업무 기록을 모아 의미 기반으로 검색하고,
**출처가 붙은 답변**을 메신저 봇으로 돌려주는 RAG 챗봇입니다.

```
[Notion]  ─┐
[Slack]   ─┼─→ FastAPI (병렬 수집 → 청킹 → 임베딩) → 벡터 DB
[KakaoWork]┘                                          ↓ 유사도 검색
                                                   Claude API
                                                      ↓
                                        출처가 명시된 답변 → 메신저 봇
```

> 세 소스는 **각각 독립적으로 직접** 벡터 DB에 들어갑니다.
> Notion을 다른 소스의 중간 경유지로 쓰지 않습니다.

---

## 처음 클론했다면 (약 40분)

4번 설치가 5~15분 걸립니다. **먼저 걸어두고** 나머지를 읽으세요.

```bash
# 1. Python 3.11 이상인지 확인
python --version

# 2. 가상환경 만들고 활성화
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate

# 3. 패키지 설치 (오래 걸립니다. 정상입니다)
pip install -r requirements.txt

# 4. 환경변수 파일 만들고 키 채우기
copy .env.example .env          # macOS/Linux: cp .env.example .env

# 5. 서버 실행
uvicorn app.main:app --reload --port 8000
```

브라우저에서 `http://localhost:8000/health`가 응답하면 성공입니다.

**막히면 혼자 붙들지 마세요.** 에러 메시지 전문을 팀 채널에 그대로 붙여넣으면 됩니다.

---

## 폴더 구조

```
app/
├── main.py              FastAPI 엔트리포인트
├── core/                설정(config.py), 서명 검증(security.py)
├── api/                 Slack·KakaoWork 이벤트 수신, 헬스체크
├── services/            수집·임베딩·벡터DB·Claude 호출·QA 파이프라인
├── scheduler/           주기 수집, 변경 감지
└── models/              Pydantic 스키마
tests/
```

`app/` 아래 5개 분류를 벗어나는 새 최상위 폴더는 만들지 않습니다.

---

## 팀과 담당

| 이름 | 담당 |
|---|---|
| 임혜랑 (팀장) | KakaoWork, Notion 수집 |
| 마준서 | KakaoWork, 임베딩·벡터 DB |
| 이인아 | Slack, Claude 연동 |
| 송준호 | Slack 수집, 테스트 |

`services/vector_store.py`와 `services/qa_pipeline.py`는 **모두가 쓰는 공용 파일**입니다.
고칠 때는 반드시 PR로 공유하세요.

---

## 협업 규칙

- 브랜치: `main`(배포) / `develop`(통합) / `feature/기능명`(작업)
- 커밋 접두사: `feat:` `fix:` `docs:` `refactor:` `test:`
- PR은 최소 1명 리뷰 후 `develop`에 병합
- **코칭 회차(8/12, 8/26, 9/9) 전날까지 최신 코드를 push**

```bash
git checkout develop && git pull
git checkout -b feature/notion-collect
# ... 작업 ...
git add -A && git commit -m "feat: Notion 페이지 수집 구현"
git push -u origin feature/notion-collect
```

---

## 반드시 지킬 것

- **API 키는 환경변수로만.** 코드에 문자열로 넣지 않습니다
- **`.env`는 커밋 금지.** `.gitignore`에 등록돼 있으니 `git status`에 뜨면 뭔가 잘못된 것입니다
- 외부 API 호출에는 `try/except` + 재시도를 넣습니다
- 벡터 DB에 넣는 조각에는 `source`, `url`, `title`, `created_at`을 반드시 붙입니다 — **인용 기능이 여기에 의존합니다**
- 메신저·문서에 섞인 연락처 같은 개인정보는 수집 단계에서 걸러냅니다

---

## 자주 막히는 지점

| 증상 | 해결 |
|---|---|
| Notion 403 | 대상 DB가 Integration과 연결되지 않음. Notion 페이지 `⋯` → 연결 → Integration 추가 |
| `notion_api_key field required` | `.env`가 없거나, 프로젝트 루트가 아닌 곳에서 실행함 |
| `ModuleNotFoundError: app` | 프로젝트 루트에서 실행하지 않았거나 가상환경 미활성화 |
| Claude 404 | 모델 문자열 오타. 모델명은 `core/config.py` 한 곳에서만 관리 |
| Slack 이벤트 중복 | 3초 안에 응답 못 함. 즉시 200 반환 후 백그라운드 처리 |
| `pip install`이 너무 느림 | 정상입니다. sentence-transformers와 torch가 큽니다 |

---

## 실행 명령 모음

```bash
uvicorn app.main:app --reload --port 8000     # 개발 서버
python -m app.services.notion_service          # Notion 수집 단독 확인
pytest -q                                      # 테스트 전체
```

---

## 문서

| 문서 | 내용 |
|---|---|
| [CLAUDE.md](./CLAUDE.md) | 확정 아키텍처, 기술 스택, 코딩 규칙 (변경 금지 항목 포함) |
| [TEAM_CHECKLIST.md](./TEAM_CHECKLIST.md) | 주차별 실행 체크리스트와 역할 분담 |

일정·회의록의 원본은 Notion 워크스페이스입니다.
