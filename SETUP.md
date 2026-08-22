# 팀원 설치 안내 (SETUP)

**대상**: 마준서 · 이인아 · 송준호
**목표**: 각자 노트북에서 챗봇에게 질문하고 출처가 붙은 답을 받아보는 것
**걸리는 시간**: 처음이면 약 30분 (대부분 다운로드 대기)

막히면 혼자 붙들지 말고 **에러 메시지 전문**을 팀 채널에 그대로 붙여넣으세요.
아래 「자주 막히는 지점」에 대부분 나와 있습니다.

---

## 0. 팀장에게 미리 받을 것

| 받을 것 | 용도 |
|---|---|
| 키 3개 (`NOTION_API_KEY`, `NOTION_ROOT_PAGE_ID`, `ANTHROPIC_API_KEY`) | `.env` 파일에 채웁니다 |
| 저장소 접근 권한 | private 저장소라 초대를 수락해야 clone됩니다 |

**실제로 채울 값은 이 3개뿐입니다.** `KAKAOWORK_APP_KEY`는
`.env.example`의 자리표시자 그대로 두면 됩니다. 아직 그 기능을 안 쓰는데도
값이 비면 프로그램이 안 뜨기 때문에 넣어둔 것뿐입니다.
**카카오워크 키를 기다리지 말고 지금 시작하세요.**

키는 **팀 공용 계정 1개**를 씁니다. 개인 키를 새로 발급받지 마세요.
개인 Notion 키를 쓰면 우리 페이지에 접근 권한이 없어서 `403`이 납니다.

---

## 1. 처음 받는 경우

```powershell
# 1) 코드 받기
git clone https://github.com/hellryang/simsreality-rag-bot.git
cd simsreality-rag-bot

# 2) 가상환경 만들고 켜기
python -m venv .venv
.venv\Scripts\activate

# 3) 패키지 설치 (5~15분 걸립니다. 정상입니다)
pip install -r requirements.txt

# 4) 환경변수 파일 만들기
copy .env.example .env
notepad .env          # 팀장이 준 키를 채우고 저장

# 5) 벡터 DB 만들기 (첫 실행은 모델 500MB 다운로드로 오래 걸립니다)
python build_db.py
```

기본 브랜치가 `develop`이라 clone하면 바로 최신 통합 코드를 받습니다.

---

## 2. 이미 받아둔 경우 (업데이트)

```powershell
cd <프로젝트 폴더>
.venv\Scripts\activate

git checkout develop
git pull

pip install -r requirements.txt     # 버전이 고정돼 있으니 그대로 맞춰집니다
python build_db.py --reset          # 수집 방식이 바뀌었으므로 --reset 필요
```

`--reset`을 빼면 예전 방식으로 들어간 낡은 조각이 남아 검색이 이상해집니다.

---

## 3. ⚠️ git에 없는 것이 두 개 있습니다

여기서 대부분 막힙니다. **코드를 받아도 이 둘은 안 딸려옵니다.**

### `.env` — 각자 만들어야 합니다

API 키가 들어 있어서 저장소에 절대 올리지 않습니다(`.gitignore` 등록).
`copy .env.example .env` 후 팀장이 준 값을 채우세요.

### `chroma_data/` — 각자 만들어야 합니다

벡터 DB(검색 대상 데이터)입니다. 용량이 크고 각자 갱신 시점이 달라서
저장소에 넣지 않습니다. **`python build_db.py`를 직접 한 번 돌려야 합니다.**

이걸 안 하면 질문해도 이렇게 나옵니다.

```
찾은 조각: 0건
```

**코드가 고장난 게 아닙니다.** 검색할 데이터가 아직 없다는 뜻입니다.

---

## 4. 잘 됐는지 확인하기

```powershell
python -m app.services.qa_pipeline --chat
```

```
임베딩 모델을 준비합니다. 처음 한 번만 걸립니다...

벡터 DB에 조각 31개가 있습니다.        ← 0이 아니면 성공

질문> ?카카오워크 담당
찾은 조각: 5건
  유사도 0.502  [2026 일경험 프로젝트(2026-07.27~09.18)]
    이름: 임혜량(팀장) | 학년: 2학년 | 연락처: [연락처] | ...
```

이 화면이 나오면 성공입니다. **캡처해서 팀 채널에 올려주세요.**

> **`?`를 붙이면 검색만 합니다.** Claude를 부르지 않으므로 **요금이 안 나갑니다.**
> 처음 확인할 때는 `?`를 붙여서 하세요. 팀 공용 계정 하나로 요금이 나가므로
> 이것저것 시험해볼 때도 `?`를 쓰는 습관이 좋습니다.

`?` 없이 물으면 Claude가 문장으로 답하고 출처가 붙습니다.

```
질문> 카카오워크 담당은 누구야
카카오워크 담당은 임혜량(팀장)과 마준서입니다. [1]

출처:
  [1] 2026 일경험 프로젝트(2026-07.27~09.18) — https://app.notion.com/p/...
```

---

## 5. 평소 쓰는 명령

터미널을 새로 열 때마다 **가상환경 켜기부터** 합니다. 창을 닫으면 풀립니다.

```powershell
cd <프로젝트 폴더>
.venv\Scripts\activate               # 앞에 (.venv) 가 붙으면 성공

python -m app.services.qa_pipeline --chat     # 대화형 (여러 번 물어볼 때 편함)
python -m app.services.qa_pipeline "질문"      # 한 번만 물어볼 때
python build_db.py --reset                     # 노션 문서가 바뀌었을 때
pytest -q                                      # 테스트
```

대화형 모드에서 `exit` 또는 `Ctrl+C`로 나옵니다.

명령별 자세한 설명과 출력 예시는 [RUNBOOK.md](./RUNBOOK.md)에 있습니다.

---

## 6. 자주 막히는 지점

| 증상 | 원인 | 해결 |
|---|---|---|
| `ModuleNotFoundError: notion_client` | 가상환경이 꺼져 있음 | `.venv\Scripts\activate` |
| `ModuleNotFoundError: app` | 프로젝트 루트가 아닌 곳에서 실행 | 프로젝트 폴더로 `cd` |
| `ValidationError: notion_api_key field required` | `.env`가 없음 | `copy .env.example .env` 후 키 입력 |
| `찾은 조각: 0건` | 벡터 DB가 비어 있음 | `python build_db.py` 실행 |
| Notion `403` / `404` | 개인 키를 썼거나 페이지 미공유 | 팀 공용 Notion 키인지 확인 |
| Claude 인증 실패 | `.env`의 키가 `sk-ant-xxx` 자리표시자 그대로 | 진짜 키로 교체 |
| `pip install`이 너무 느림 | 정상 | sentence-transformers와 torch가 큽니다 |
| 표 내용이 검색이 안 됨 | 노션 표의 **머리글 행**이 꺼져 있음 | 표 좌상단 `⋮⋮` → 머리글 행 켜기 → `build_db.py --reset` |
| 답변에 한 명이 빠짐 | 한 문장에 주제를 둘 넣음 | **질문은 한 번에 한 주제씩** |

---

## 7. 하지 말 것

- **`.env`를 커밋하지 마세요.** `git status`에 `.env`가 뜨면 뭔가 잘못된 것입니다
- **`develop`에 직접 push하지 마세요.** `feature/기능명` 브랜치를 만들고 PR로 올립니다
- **`app/models/schemas.py`, `app/services/vector_store.py`, `app/services/qa_pipeline.py`,
  `app/services/embedder.py`는 공용 파일입니다.** 고치면 다른 사람 코드가 조용히 깨집니다.
  고쳐야 하면 팀 채널에 먼저 올리세요
- **`tests/test_contracts.py`를 통과시키려고 테스트를 고치지 마세요.** 계약을 바꿔야
  한다고 판단되면 팀 합의 후 별도 PR로 냅니다

---

## 8. 지금 어디까지 되어 있나

| 기능 | 상태 |
|---|---|
| Notion 문서 수집 (하위 페이지, 표, 토글) | 됨 |
| 연락처·이메일 마스킹 | 됨 |
| 의미 검색 + 출처 붙은 Claude 답변 | 됨 |
| 배포 (Railway/Render) | **미구현** (임혜량) |
| KakaoWork 수집·발송·봇 | **미구현** (마준서) |
| 관리자 대시보드 (UI / API) | **미구현** (이인아 / 송준호) |
| 주기 수집 (APScheduler) | **미구현** |

**Slack은 2026-08-22에 범위에서 빠졌습니다.** 코드에 남아 있는
`app/services/slack_service.py`는 `scrub_pii`를 다시 내보내는 한 줄짜리 파일이라
지우면 테스트가 깨집니다. **그대로 두세요.**

각자 담당은 [TEAM_CHECKLIST.md](./TEAM_CHECKLIST.md)를 보세요.

---

## 참고 문서

| 문서 | 내용 |
|---|---|
| [README.md](./README.md) | 프로젝트 소개 |
| [RUNBOOK.md](./RUNBOOK.md) | 명령어별 상세 설명, 출력 예시 |
| [CLAUDE.md](./CLAUDE.md) | 확정 아키텍처, 기술 스택, 코딩 규칙 |
| [TEAM_CHECKLIST.md](./TEAM_CHECKLIST.md) | 주차별 체크리스트, 역할 분담 |
