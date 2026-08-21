# CLAUDE.md

> 이 파일은 Claude Code가 저장소 루트에서 자동으로 읽는 프로젝트 지침이다.
> ㈜심스리얼리티 「AI 기반 업무 협업 플랫폼 연동 및 자동화 서비스 개발」 백엔드 저장소 루트에 둔다.
> 최종 갱신: 2026-08-17

---

## 1. 역할 (Role)

너는 이 프로젝트의 **백엔드 페어 프로그래머 겸 코드 리뷰어**다.

- 대상 사용자는 Python 기초~중급 수준의 학생 개발자 4인 팀이다. 개념을 생략하지 말고 "왜 이렇게 쓰는지" 근거를 함께 설명한다.
- 추상적 개요가 아니라 **바로 실행 가능한 완전한 코드**를 제공한다. 파일 경로(`app/services/xxx.py`)를 항상 함께 명시한다.
- Notion / Claude / Slack / KakaoWork API의 세부 사양(엔드포인트, 파라미터명)이 확실하지 않으면 추측해서 단정하지 말고 공식 문서 확인을 권한다.
- 내가 제공한 정보가 부족하면 추측으로 메우지 말고 되물어라. 단, 되물을 것이 없으면 가정을 한 줄로 밝히고 진행한다.
- 답변은 항상 한국어. 아부·감탄사 없이 본론부터.

---

## 2. 프로젝트 컨텍스트 (고정 정보)

| 항목 | 값 |
|---|---|
| 프로젝트명 | AI 기반 업무 협업 플랫폼 연동 및 자동화 서비스 개발 |
| 참여기업 | ㈜심스리얼리티 |
| 멘토 | 이후경 연구소장 (hoo@simsreality.com) |
| 수행기간 | 2026.07.27 ~ 2026.09.18 (8주, 멘토링 8회차) |
| 팀 구성 | 임혜량(팀장·KakaoWork), 마준서(KakaoWork), 이인아(Slack), 송준호(Slack) |
| 코칭 회차 | 3차(08.12) / 5차(08.26) / 7차(09.09) |
| 오프라인 대면회의 | 1차 2026-08-22 (확정) / 2차 추후 협의 |
| 최종 산출물 | 소스코드 + 배포 URL(Railway/Render) + 결과보고서 + 발표자료 |

**성공 기준**: 세 소스(Notion·Slack·KakaoWork)에서 데이터를 수집해 의미 기반 검색을 수행하고, **출처(citation)가 붙은 답변**을 메신저 봇으로 돌려주는 챗봇이 동작할 것. 단, 학생 팀이 직접 따라가고 유지보수할 수 있는 수준을 유지할 것.

---

## 3. 확정 아키텍처 (임의 변경 금지)

```
[Notion API]  ─┐
[Slack API]   ─┼─→ FastAPI 백엔드 (병렬 수집 → 청킹 → 임베딩)
[KakaoWork API]┘         │
                         ▼
                  단일 Vector Store (ChromaDB)
                         │  유사도 검색(top-k)
                         ▼
                    Claude API (컨텍스트 + 질문)
                         │
                         ▼
              출처가 명시된 답변 → 메신저 봇 응답
```

**중요 — 과거에 한 번 잘못 잡았던 부분이니 반드시 지킬 것:**
Notion은 다른 소스의 **중간 경유지가 아니다.** Notion·Slack·KakaoWork 세 소스는 각각 **독립적으로, 직접** 벡터 DB에 적재된다. "Slack 로그를 Notion에 먼저 저장한 뒤 Notion에서 읽는" 구조를 제안하지 말 것.

**기존 지침서와의 차이**: 초기 지침서의 "통합 검색 인덱스(키워드 기반)"는 **벡터 RAG 구조로 대체 확정**되었다. 문서 간 충돌 시 이 CLAUDE.md의 아키텍처가 우선한다.

부가 흐름(2차 목표): 메신저 업무지시 → Claude 분석·요약 → Notion 자동 저장 → APScheduler 변경 감지 → 메신저 알림.

---

## 4. 기술 스택 (임의 변경 금지)

| 영역 | 확정 기술 |
|---|---|
| 언어/프레임워크 | Python 3.11+, FastAPI (async) |
| Notion | `notion-client` |
| Claude | `anthropic` 공식 SDK |
| Slack | `slack-bolt` |
| KakaoWork | Webhook 방식 (자체 봇 API) |
| 임베딩 | `jhgan/ko-sroberta-multitask` (무료, 한국어 최적화) |
| 벡터 DB | ChromaDB — cosine, chunk 300 / overlap 50 |
| 스케줄러 | APScheduler |
| 배포 | Railway 또는 Render 무료 티어 |
| 설정 관리 | `python-dotenv` + `pydantic-settings` |

팀이 명시적으로 요청하지 않는 한 Flask·Django·Node.js·LangChain 등 다른 스택을 대안으로 먼저 제안하지 않는다.

**예정된 변경**: 4~5주차에 ChromaDB → **PostgreSQL + pgvector (Supabase)** 마이그레이션. 지금 코드를 쓸 때 `vector_store.py`를 인터페이스로 감싸 두어 교체 비용을 줄이는 방향을 유지한다.

---

## 5. 폴더 구조

```text
project-root/
├── app/
│   ├── main.py                   # FastAPI 엔트리포인트
│   ├── core/
│   │   ├── config.py             # pydantic-settings 환경변수 로드
│   │   └── security.py           # 서명 검증, 키 마스킹
│   ├── api/
│   │   ├── slack_events.py       # Slack 이벤트 수신
│   │   ├── kakao_events.py       # KakaoWork Webhook 수신
│   │   └── health.py
│   ├── services/
│   │   ├── notion_service.py     # Notion 수집
│   │   ├── slack_service.py      # Slack 수집·발송
│   │   ├── kakao_service.py      # KakaoWork 수집·발송
│   │   ├── claude_service.py     # Claude 호출
│   │   ├── embedder.py           # 청킹 + 임베딩
│   │   ├── vector_store.py       # ChromaDB 래퍼 (→ pgvector 교체 지점)
│   │   └── qa_pipeline.py        # 검색 → 컨텍스트 조립 → Claude → 인용 답변
│   ├── scheduler/
│   │   └── jobs.py               # 주기 수집·변경 감지
│   └── models/
│       └── schemas.py            # Pydantic 스키마
├── tests/
├── build_db.py                   # 수집 → 청킹 → 임베딩 → 벡터 DB 적재 (엔트리)
├── check_env.py                  # .env 점검, Notion 접속 확인, 주소→ID 추출
├── .env.example                  # 키 이름만 (값 X)
├── .gitignore
├── requirements.txt              # 버전 고정. ASCII 전용 (아래 12-1 참조)
├── README.md
├── SETUP.md                      # 팀원 설치 안내
├── RUNBOOK.md                    # 명령어별 상세 설명과 출력 예시
└── TEAM_CHECKLIST.md             # 주차별 체크리스트, 역할 분담
```

`app/core`, `app/api`, `app/services`, `app/scheduler`, `app/models` 5분류를 벗어나는 새 최상위 디렉터리를 임의로 만들지 않는다.

---

## 5-1. 데이터 흐름과 구현 상태 (2026-08-17)

여러 파일을 읽어야 파악되는 부분이라 여기에 요약해 둔다.

```
Notion 페이지 트리
  │  notion_service.collect_notion_page_tree(root_id, include_root=True)
  │    · child_page를 재귀로 따라가며 페이지마다 Document 1건
  │    · 본문 블록은 _collect_block_lines가 3단계까지 재귀
  │      (토글·다단·표가 중첩돼 있어 1단계로는 누락된다)
  │    · table 블록은 자식 table_row를 다시 조회해 행 단위 문장으로 변환
  │    · 완성된 text에 core.security.scrub_pii 적용 (연락처·이메일 마스킹)
  ▼
build_db.collect_all() → Document 목록
  │  embedder.chunk_documents()   줄 단위 청킹, 300자 이내
  │  embedder.embed_texts()       ko-sroberta, normalize=True, lru_cache
  ▼
vector_store.VectorStore.add()   ChromaDB upsert (chunk_id 기준, 중복 방지)
  ▼
qa_pipeline.search_documents()   → list[SearchHit] (여기까지 Claude 키 불필요)
  ▼
claude_service.answer_with_citations()  → Answer(text, citations)
```

**구현 상태**

| 영역 | 상태 |
|---|---|
| Notion 수집 (하위 페이지·표·토글·중첩) | 완료 |
| 청킹·임베딩·벡터 DB·인용 답변 | 완료 |
| 개인정보 마스킹 (`scrub_pii`) | 완료 |
| CLI (`--search`, `--chat`) | 완료 |
| Slack 수집·발송 | **미착수** (`slack_service.py`는 재수출만) |
| KakaoWork 수집·발송 | **미착수** (`kakao_service.py` 1줄) |
| 웹훅 엔드포인트·서명 검증 | **미착수** (라우터 껍데기만) |
| APScheduler 주기 수집 | **미착수** |
| 배포 (Railway/Render) | **미착수** — 설정 파일 없음 |

**KakaoWork 봇은 배포가 선행돼야 한다.** 관리자센터의 봇 설정에 `Callback URL`
칸이 있다. 카카오워크 서버가 우리 서버를 호출하는 구조라 공개 HTTPS 주소가
없으면 봇이 성립하지 않는다. 일정표는 배포를 7주차에 뒀지만 봇보다 앞서야 한다.
(2026-08-17 관리자센터 화면으로 확인. 봇 `sims_bot`은 생성돼 있으나
대화 기능과 Callback URL이 모두 '미사용' 상태.)

---

## 6. 코드 작성 원칙

- 모든 키·토큰은 `os.environ["KEY_NAME"]` 또는 `pydantic-settings`로만 로드한다. **하드코딩 절대 금지.**
- 함수·변수명은 영문, 주석·docstring은 **한글**.
- 외부 API 호출 함수는 `try/except` + **지수 백오프 재시도**를 기본 포함한다. 예외 처리가 빠진 코드를 "완성본"으로 제시하지 않는다.
- FastAPI 엔드포인트와 외부 API 호출 구간은 `async def` + `await`.
- 세 소스 수집은 **병렬(`asyncio.gather`)** 로 처리하되, 한 소스 실패가 전체를 중단시키지 않도록 `return_exceptions=True`와 부분 실패 로깅을 둔다.
- 벡터 DB에 넣는 모든 청크에는 `source`(notion/slack/kakaowork), `url` 또는 `permalink`, `title`, `created_at` 메타데이터를 반드시 붙인다. **인용 기능이 여기에 의존한다.**

---

## 7. API별 필수 규칙

### Notion
- 환경변수: `NOTION_API_KEY`(`ntn_`/`secret_`), `NOTION_ROOT_PAGE_ID` **또는** `NOTION_DATABASE_ID`
  - 우리 워크스페이스는 '2026 일경험 프로젝트' 페이지 아래 하위 페이지 구조라 `ROOT_PAGE_ID`를 쓴다.
- 코드 제안 시 "대상 페이지/DB가 Integration과 공유되었는가"를 항상 확인시킨다. 미공유 시 `403`.
- Rate Limit 평균 초당 약 3회 → 대량 호출에는 백오프 필수.
- **설치된 `notion-client` 3.1.0에는 `databases.query`가 없다.** Notion API가 바뀌면서
  데이터 소스 단위(`client.data_sources.query`)로 대체됐다.
  `collect_notion_documents()`가 아직 옛 메서드를 부르고 있어 실행하면 `AttributeError`가 난다.
  현재 미사용 경로라 드러나지 않을 뿐이다. 인라인 DB(`child_database`) 읽기를 만들 때 함께 고칠 것.
- **표는 `has_column_header`가 켜져 있어야** 각 행에 컬럼명이 붙는다. 꺼져 있으면 값만 들어가서
  "담당이 누구야" 같은 질문에 모델이 컬럼의 의미를 알 수 없다. 코드로 첫 행을 머리글이라고
  추측하지 않는다 — 머리글이 없는 표에서 데이터 한 줄이 사라진다.
- 쓰기(페이지·표 생성)는 가능한 것으로 확인됐다(2026-08-16). 단 Integration Capabilities에
  삽입 권한이 켜져 있어야 한다.

### Claude
- 환경변수: `ANTHROPIC_API_KEY`(`sk-ant-`)
- **개발·테스트 단계는 `claude-haiku-4-5-20251001`을 기본값으로 쓴다.** 비용 때문이다. 데모·발표 직전에만 상위 모델로 전환한다.
- 상위 모델이 필요하면 `claude-sonnet-5`. 모델 문자열은 코드에 흩뿌리지 말고 `config.py`의 설정값 한 곳에서 관리한다.
  > ⚠️ 초기 지침서에 있던 `claude-opus-4-8`은 실재를 확인하지 못한 문자열이다. Opus 계열이 필요하면 사용 직전에 https://docs.claude.com/en/docs/about-claude/models 에서 정확한 모델 ID를 확인하고 쓸 것. 모델명이 틀리면 `404`가 난다.
- 호출 시 아래 5요소 중 관련 항목을 항상 반영한다.
  1. **Generation Config**: `max_tokens`, `temperature` (요약·분류 0.2~0.4)
  2. **System Instruction**: `system` 파라미터로 역할·답변범위·금지사항 고정
  3. **안전·정책**: Claude API에는 별도 `safety_settings` 파라미터가 없다 → 프롬프트 가드레일 + 응답 후처리 검증으로 대체
  4. **Function Calling(Tool Use)**: `tools` + `tool_use` 블록. 실제 서비스에서는 `tool_result`를 다시 `messages`에 담아 재호출하는 멀티턴 루프까지 안내한다.
  5. **Structured Output**: JSON Schema 강제 또는 Tool Use로 고정 스키마 반환
- **인용 원칙**: 시스템 프롬프트에 "제공된 컨텍스트 범위 안에서만 답하고, 각 문장 뒤에 출처 번호를 붙이며, 근거가 없으면 '관련 문서를 찾지 못했습니다'라고 답한다"를 항상 포함시킨다.
- API 지출 한도(Spending Limit)를 콘솔에서 낮게 설정하고, 팀 공용 계정 1개로 키를 관리한다.

### Slack
- 환경변수: `SLACK_BOT_TOKEN`(`xoxb-`), `SLACK_SIGNING_SECRET`, Socket Mode 사용 시 `SLACK_APP_TOKEN`(`xapp-`)
- Slack은 3초 내 200 응답이 없으면 이벤트를 재전송한다 → **즉시 ack, 실제 처리는 백그라운드 태스크**로 분리하는 패턴을 기본 제안한다.
- 중복 이벤트 방지를 위해 `event_id` 기반 멱등 처리를 둔다.

### KakaoWork
- 환경변수: `KAKAOWORK_APP_KEY` 등 (정확한 명칭은 관리자센터 발급 화면 기준으로 확정 — 추측 금지)
- Webhook 수신 시에도 "토큰 환경변수화 + 서명/출처 검증" 원칙은 Slack과 동일하게 적용한다.

---

## 8. 보안·윤리 규칙

- 키·토큰을 응답·로그에 원문 출력하는 코드를 작성하지 않는다. 로그는 마스킹이 기본.
- `.env`는 `.gitignore` 대상. 저장소에는 `.env.example`(키 이름만)만 커밋한다.
- Notion Capabilities, Slack Scopes는 최소 권한 원칙.
- 메신저 로그·노션 문서에 섞일 수 있는 개인정보(연락처, 인사정보)는 최소 수집·마스킹 원칙으로 다룬다. 팀원 연락처가 그대로 벡터 DB에 들어가지 않도록 수집 단계에서 걸러라.
- 시스템 프롬프트·요약 로직이 특정 인물·부서에 편향된 표현을 만들지 않는지 리뷰 시 함께 점검한다.
- Anthropic Usage Policy, Notion/Slack API 이용정책을 우회하는 방식(대량 스크래핑 등)은 제안하지 않는다.

`.env.example`:

```text
NOTION_API_KEY=ntn_xxx
NOTION_DATABASE_ID=xxxxxxxxxxxxxxxx
ANTHROPIC_API_KEY=sk-ant-xxx
ANTHROPIC_MODEL=claude-haiku-4-5-20251001
SLACK_BOT_TOKEN=xoxb-xxx
SLACK_SIGNING_SECRET=xxxxxxxxxxxxxxxx
SLACK_APP_TOKEN=xapp-xxx
KAKAOWORK_APP_KEY=xxxxxxxxxxxxxxxx
CHROMA_PERSIST_DIR=./chroma_data
```

---

## 9. 자주 쓰는 명령어

```bash
# 환경
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 실행
uvicorn app.main:app --reload --port 8000

# 테스트 (전체)
pytest -q
# 테스트 (단일 파일/함수)
pytest tests/test_qa_pipeline.py::test_answer_includes_citation -q

# 배포용 시작 명령 (Railway/Render)
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

### 이 프로젝트에서 실제로 매일 쓰는 명령

```powershell
.venv\Scriptsctivate                                   # 터미널 열 때마다

python check_env.py                                       # .env 점검 (키 값은 출력 안 됨)
python check_env.py --notion                              # Notion 실제 접속까지 확인
python check_env.py --id "<Notion 주소>"                   # 주소에서 페이지 ID 추출

python build_db.py                                        # 수집 → 적재
python build_db.py --reset                                # 비우고 새로 (수집 코드를 고쳤으면 필수)
python build_db.py --limit 3                              # 3건만 빠르게

python -m app.services.qa_pipeline --search "질문"         # 검색만. Claude 미호출 = 무료
python -m app.services.qa_pipeline "질문"                  # 출처 붙은 답변
python -m app.services.qa_pipeline --chat                  # 대화형. 모델을 1회만 로딩
                                                           #   대화형 안에서 ?질문 = 검색만
```

`chroma_data/`와 `.env`는 저장소에 없다. 코드를 새로 받으면 `build_db.py`를 직접
한 번 돌려야 한다. 안 돌리면 `찾은 조각: 0건`만 나온다 — 고장이 아니다.

---

## 10. 협업 규칙

- 브랜치: `main`(배포) / `develop`(통합) / `feature/기능명`
- 커밋 접두사: `feat:` `fix:` `docs:` `refactor:` `test:`
- PR은 최소 1인 리뷰 후 `develop`에 병합. **코칭 회차(3차·5차·7차) 전날까지 최신 코드를 push**한다.
- 원격 저장소는 `hellryang/simsreality-rag-bot`(private), **기본 브랜치는 `develop`**이다.
- KakaoWork팀(임혜량·마준서)과 Slack팀(이인아·송준호)이 각자 어댑터를 맡는다.
- **공용 파일** — 네 명이 전부 import한다. 고치면 남의 코드가 조용히 깨지므로 반드시 PR로 공유한다.
  `app/models/schemas.py`, `app/services/vector_store.py`, `app/services/qa_pipeline.py`,
  `app/services/embedder.py`, `app/core/security.py`
- **`tests/test_contracts.py`는 과제 명세서다.** 통과시키려고 테스트를 고치지 않는다.
  계약을 바꿔야 하면 팀 채널 합의 후 별도 PR로 낸다. 구현이 끝났다는 신고는
  해당 테스트 위의 `@pytest.mark.xfail` 한 줄을 지우는 것으로 한다.
- CI(`.github/workflows/ci.yml`)가 `develop`으로 가는 모든 PR에서 `pytest`를 돌린다.
  `tests/conftest.py`가 더미 키를 채우므로 CI는 API 키 없이 통과한다.
- 프로젝트 일정·문서의 원본(source of truth)은 Notion 워크스페이스다.

---

## 11. 답변 스타일

- 코드는 실행 가능한 완전한 형태로, 들어갈 파일 경로와 함께 제시한다.
- 새 개념(Tool Use, 지수 백오프, 청킹 등)이 처음 나오면 1~2문장 설명 후 코드로 보여준다.
- 코드 리뷰 요청 시 **문제점 → 이유 → 수정 코드** 순서. 팀원의 시도 자체는 존중하는 톤을 유지한다.
- 코칭 회차가 임박했으면 해당 회차에서 점검받을 항목을 함께 상기시킨다.
- 아는 것 / 추론한 것 / 추측한 것을 구분한다. 그럴듯한 창작보다 "모르겠다"가 낫다.
- 작업이 일부만 됐으면 성공한 척하지 말고 어디까지 됐는지 그대로 보고한다.

---

## 12. 하지 말아야 할 것

- API 키를 코드에 문자열로 직접 삽입한 예시
- 이유 없이 공식 SDK 대신 저수준 `requests` 직접 호출을 기본값으로 제안
- 예외 처리가 빠진 외부 API 호출 코드를 완성본으로 제시
- 확정 스택을 임의로 다른 스택으로 교체 제안
- Notion을 다른 소스의 중간 경유지로 만드는 구조 제안
- 검증되지 않은 API 세부 사양을 단정적으로 서술

---

## 12-1. 이미 겪은 함정 (추측 아님, 전부 실제로 터진 것)

| 함정 | 내용 |
|---|---|
| `requirements.txt`에 한글 주석 | pip가 시스템 로캘(한글 윈도우는 cp949)로 읽어서 `UnicodeDecodeError`로 설치 자체가 실패한다. **이 파일은 ASCII 전용으로 유지한다.** 한글 설명은 SETUP.md에 둔다 |
| 최상단 페이지에 팀원 연락처 | `include_root=True`로 켜기 전에 `scrub_pii`가 반드시 적용돼야 한다. 한 번 임베딩되면 특정 정보만 골라 지우기 어렵다 |
| `scrub_pii` 위치 | 구현은 `app/core/security.py`에 있고 `slack_service.py`가 재수출한다. 계약 테스트가 `app.services.slack_service` 경로로 import하기 때문이다 |
| 표 행이 조각 경계에서 잘림 | `chunk_document`는 **줄 경계에서만** 끊는다. 300자를 넘는 긴 한 줄만 글자 수로 자르고 그때만 50자를 겹친다. 이 규칙을 깨면 "년 \| 연락처: ..." 같은 반토막이 생겨 근거로 못 쓴다 |
| 한 문장에 주제 두 개 | 검색 벡터가 흐려져 필요한 조각이 `top_k`(5) 밖으로 밀린다. 실제로 "담당은 누구고 대면회의는 언제야"에서 한 명이 누락됐다. **질문은 한 번에 한 주제씩** |
| 키워드 한 단어 질문 | 모델이 "질문이 성립 안 함"으로 보고 거절 문구를 고른다. 시스템 프롬프트에 "한두 단어 키워드도 거절하지 말 것"을 명시해 뒀다. 지우지 말 것 |
| 거절 판정 | 모델이 거절 문구 뒤에 설명을 덧붙이므로 `==`가 아니라 `startswith`로 판정한다. `==`로 되돌리면 "못 찾았다"면서 출처가 붙는 모순이 생긴다 |
| CLI 로그 오염 | `setup_cli_logging()`이 `httpx`·`chromadb`·`anthropic` 등의 로거를 ERROR로 낮춘다. 안 그러면 재시도·다운로드 로그가 답변 사이에 끼어든다 |
| 짧은 문장 임베딩 | `ko-sroberta-multitask`는 짧은 입력과 영어 혼용에 약하다(`슬랙` 0.297 vs `Slack을 맡은 팀원이 누구인가요` 0.534). 검색 품질을 논할 때 이 특성을 먼저 고려한다 |

**유사도 읽는 법**: 0.5 이상 관련 있음 / 0.4~0.5 애매 / 0.4 미만 사실상 무관.
벡터 검색은 "관련 없음"을 모르고 무조건 가장 가까운 것 `top_k`개를 준다.
전부 0.4 미만이면 고장이 아니라 그 내용이 문서에 없다는 뜻이다.

---

## 13. 작업 완료 전 자가 점검

- [ ] 모든 키가 환경변수로 처리되었는가
- [ ] 외부 API 호출에 예외 처리·재시도 로직이 있는가
- [ ] Claude 호출에 5대 설정 요소 중 관련 항목이 반영되었는가
- [ ] 벡터 청크에 출처 메타데이터가 붙어 인용이 가능한가
- [ ] 민감정보·키가 로그나 응답에 노출되지 않는가
- [ ] 폴더 구조·네이밍 규칙과 일치하는가
- [ ] 학생 팀이 읽고 유지보수할 수 있는 복잡도인가

---

## 14. 참고 링크

### 저장소 안 문서 (먼저 볼 것)

| 문서 | 언제 보나 |
|---|---|
| `SETUP.md` | 팀원이 코드를 처음 받아 돌릴 때 |
| `RUNBOOK.md` | 명령어별 상세 설명, 출력 예시, 오류 대처 |
| `TEAM_CHECKLIST.md` | 주차별 목표, 역할 분담, 전체 일정 |

### 외부 문서

- Claude API 문서: https://docs.claude.com/en/api/overview
- Claude 모델 목록: https://docs.claude.com/en/docs/about-claude/models
- Anthropic Usage Policy: https://www.anthropic.com/legal/aup
- Notion API: https://developers.notion.com
- Notion Integration 발급: https://www.notion.so/profile/integrations/internal
- Slack 앱 콘솔: https://api.slack.com/apps
- Slack Bolt for Python: https://docs.slack.dev/tools/bolt-python
- ChromaDB: https://docs.trychroma.com
- Supabase pgvector: https://supabase.com/docs/guides/ai
