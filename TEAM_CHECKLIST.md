# 팀 실행 체크리스트 — 3주차 (2026-08-12 3차 코칭)

> 원본은 Notion, 이 파일은 레포 안의 사본이다. 항목을 끝내면 `- [x]`로 바꿔 develop에 push한다.

## 0. 지금 상태 (2026-08-12 3차 코칭 회의 중 갱신)

| 항목 | 상태 |
|---|---|
| API 키 4종 (Notion / Claude / Slack / KakaoWork) | 발급 완료 |
| 폴더 구조 스캐폴딩 | 완료 |
| Git 저장소 + 팀원 4명 초대 | **완료** (`hellryang/simsreality-rag-bot`) |
| `app/models/schemas.py` (공용 계약) | **완료 — 테스트 8건 통과** |
| `app/services/notion_service.py` | 수집 코드 완료 + 테스트 5건 통과 (실제 API 호출 검증은 남음) |
| `tests/test_contracts.py` (이번 주 과제 명세) | **완료 — 10건이 일부러 실패 상태** |
| GitHub Actions CI | 설정 완료 (PR마다 자동 테스트) |
| `embedder` / `vector_store` / `claude_service` / `slack_service` | **docstring만 있음 → 이번 주 과제** |

---

## 1. 회의 전 — 전원 공통 (약 40분)

각자 자기 노트북에서 진행. 4번은 다운로드가 오래 걸리니 **가장 먼저 걸어두고** 나머지를 읽는다.

- [ ] `python --version` → 3.11 이상 확인 (아니면 python.org에서 3.11+ 설치)
- [ ] 팀장이 보낸 GitHub 초대 수락 후 `git clone <레포 URL>`
- [ ] 가상환경 생성·활성화
      `python -m venv .venv` → `.venv\Scripts\activate` (Windows)
- [ ] `pip install -r requirements.txt` — **sentence-transformers가 커서 5~15분 걸린다**
- [ ] `.env.example`을 복사해 `.env`로 만들고 팀장이 공유한 키 4종 채우기 (`.env`는 절대 커밋 금지)
- [ ] `uvicorn app.main:app --reload --port 8000` 실행
- [ ] 브라우저에서 `http://localhost:8000/health` 열어 응답 확인
- [ ] 성공 화면 캡처해 팀 채널에 공유 (전원 성공 = 오늘의 1차 목표)

막히면 혼자 붙들지 말고 즉시 팀 채널에 **에러 메시지 전문**을 붙여넣는다.

---

## 2. 회의 전 — 개인별

### 임혜량 (팀장 · KakaoWork)

- [ ] GitHub **private** 레포 생성 후 팀원 3명 초대
- [ ] `git init` → 첫 커밋 → push
      ```bash
      git init && git add -A && git commit -m "chore: initial scaffold"
      git branch -M main && git remote add origin <레포 URL> && git push -u origin main
      git checkout -b develop && git push -u origin develop
      ```
- [ ] GitHub 설정에서 **기본 브랜치를 develop으로 변경**
- [ ] `git status`에 `.env`가 안 뜨는지 확인 (뜨면 `.gitignore` 점검)
- [ ] Notion에서 대상 데이터베이스 우측 상단 `⋯` → **연결** → 우리 Integration 추가
      (이 단계를 빼먹으면 코드가 맞아도 403이 난다. 가장 흔한 실수)
- [ ] `python -m app.services.notion_service` 실행 → 페이지 5건 출력 확인
- [ ] 출력 화면 캡처 (**회의 데모 자료**)
- [ ] 회의 안건 정리 (아래 4번 참고)

### 마준서 (KakaoWork)

- [ ] 공통 세팅 완료
- [ ] 임베딩 모델 미리 내려받기 (첫 실행 때 약 500MB 다운로드 → 미리 받아두면 회의 후 바로 작업 가능)
      ```python
      from sentence_transformers import SentenceTransformer
      SentenceTransformer("jhgan/ko-sroberta-multitask")
      ```
- [ ] KakaoWork 관리자센터에서 **환경변수 이름·발급 방식 확인**
      (`KAKAOWORK_APP_KEY`가 실제 명칭인지, Webhook URL은 어디서 나오는지 — 추측하지 말고 화면 캡처)
- [ ] 확인 결과를 회의에서 공유

### 이인아 (Slack)

- [ ] 공통 세팅 완료
- [ ] `https://api.slack.com/apps`에서 앱 생성 여부 확인, **Bot Token Scopes** 목록 캡처
      (최소 권한 원칙 — 당장 필요한 건 `channels:history`, `channels:read`, `chat:write`)
- [ ] 앱을 실제 테스트 채널에 초대했는지 확인
- [ ] "Slack은 3초 안에 200을 못 받으면 같은 이벤트를 다시 보낸다"는 제약 확인
      → 즉시 ack 후 백그라운드 처리 구조가 필요한 이유

### 송준호 (Slack)

- [ ] 공통 세팅 완료
- [ ] `pytest -q` 실행 → 테스트 0건이라도 에러 없이 끝나는지 확인
- [ ] Slack 대화 이력 수집에 쓸 API 확인 (`conversations.history` / `conversations.list`)
      필요한 파라미터와 응답 필드 정리
- [ ] 수집한 메시지에서 **연락처·인사정보 같은 개인정보를 어떻게 걸러낼지** 초안 메모
      (벡터 DB에 그대로 들어가면 안 됨)

---

## 3. 회의에서 확인받을 것

- [ ] 확정 아키텍처가 맞는지 재확인
      → 세 소스를 **각각 독립적으로** 벡터 DB에 적재. Notion을 경유지로 쓰지 않음
- [ ] 답변 품질 기준: 출처 몇 개까지 붙일지, 근거 없을 때 문구를 어떻게 할지
- [ ] KakaoWork Webhook 방식으로 양방향 대화가 가능한지 (수신만 되는지)
- [ ] 배포 대상: Railway vs Render 중 어느 쪽으로 갈지
- [ ] pgvector 마이그레이션 시점 (4~5주차 유지 여부)
- [ ] 데모 시연: `/health` 응답 + Notion 수집 결과

---

## 4. 이번 주 남은 작업 (8/13 ~ 8/16)

**이번 주부터는 "알아서 만들기"가 아니라 "정해진 테스트 통과시키기"다.**
`tests/test_contracts.py`에 각자 만들어야 할 함수의 이름·인자·반환값이 이미
코드로 적혀 있다. 4명이 동시에 작업해도 주말 통합 때 안 맞는 사고를 막으려는 것이다.

### 작업 절차 (전원 동일)

```bash
git checkout develop && git pull
git checkout -b feature/기능명

pytest tests/test_contracts.py -q        # 지금은 x(예상된 실패)로 뜬다
# ... 담당 파일 구현 ...
pytest -q                                # 통과할 때까지

# 통과하면 그 테스트 위의 @pytest.mark.xfail(...) 한 줄을 지운다
pytest -q                                # 이제 . (통과)로 바뀐다

git add -A && git commit -m "feat: 청킹·임베딩 구현"
git push -u origin feature/기능명         # → GitHub에서 develop으로 PR
```

`xfail` 한 줄을 지우는 것이 **"구현 끝났다"는 신고**다. 안 지우면 CI가 실패시킨다.

### 담당

| 담당 | 파일 | 통과시켜야 할 테스트 |
|---|---|---|
| 임혜량 | `notion_service.py` | (테스트 통과 완료) 실제 Notion API로 전체 수집 검증 + `build_db.py` |
| 마준서 | `embedder.py`, `vector_store.py` | `chunk_document` 3건, `embed_texts` 1건, `VectorStore` 2건 |
| 이인아 | `claude_service.py` | `build_system_prompt` 1건, `answer_with_citations` 1건 |
| 송준호 | `slack_service.py` | `scrub_pii` 3건 + Slack 수집 함수 |

> **테스트를 고쳐서 통과시키지 않는다.** 계약을 바꿔야 한다고 판단되면
> 먼저 팀 채널에 올려 합의한 뒤, `test_contracts.py`를 고치는 PR을 따로 낸다.

**주말 전 통합 목표**: 루트의 `build_db.py`로 Notion 문서를 ChromaDB에 넣고,
`qa_pipeline.py`가 질문 하나에 출처 붙은 답변을 돌려주는 것.

### 공용 파일 규칙

`app/models/schemas.py`는 4명 전원이 import한다. 여기를 고치면 남의 코드가
조용히 깨진다. 고쳐야 하면 **PR + 팀 채널 공유가 필수**다.
`vector_store.py`와 `qa_pipeline.py`도 같은 취급.

---

## 5. 전체 일정

| 주차 | 기간 | 목표 |
|---|---|---|
| 1~2주 | 07/27 ~ 08/09 | 환경 세팅, API 키 발급 — 완료 |
| **3주** | **08/10 ~ 08/16** | **레포·환경 통일, Notion 수집, ChromaDB 적재 (3차 코칭 08/12)** |
| 4주 | 08/17 ~ 08/23 | Claude 인용 답변, Slack·KakaoWork 봇 연결 |
| 5주 | 08/24 ~ 08/30 | 3소스 통합 검색, pgvector 마이그레이션 (5차 코칭 08/26) |
| 6주 | 08/31 ~ 09/06 | APScheduler 주기 수집, Notion 자동 저장 |
| 7주 | 09/07 ~ 09/13 | Railway/Render 배포, 안정화 (7차 코칭 09/09) |
| 8주 | 09/14 ~ 09/18 | 결과보고서 + 발표자료 |

---

## 6. 자주 막히는 지점

| 증상 | 원인과 해결 |
|---|---|
| Notion 403 | 대상 DB가 Integration과 연결되지 않음. Notion 페이지 `⋯` → 연결 → Integration 추가 |
| `ValidationError: notion_api_key field required` | `.env`가 없거나 실행 위치가 프로젝트 루트가 아님 |
| `ModuleNotFoundError: app` | 프로젝트 루트에서 실행하지 않았거나 가상환경 미활성화 |
| Claude 404 | 모델 문자열 오타. `config.py`의 `anthropic_model` 한 곳만 사용 |
| Slack 이벤트 중복 | 3초 내 ack 미응답. 즉시 200 반환 후 백그라운드 처리 + `event_id` 멱등 처리 |
| `pip install` 매우 느림 | 정상. sentence-transformers와 torch가 큼 |

**공통 원칙**: 키는 환경변수로만. 하드코딩 금지. `.env`는 커밋 금지.
