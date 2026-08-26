# 실행 안내서 (RUNBOOK)

터미널에서 무엇을 어떤 순서로 치는지 정리한 문서다.
명령을 잊었을 때 이 파일만 열면 된다.

> 이 문서는 2026-08-12 3차 코칭에서 실제로 돌려본 순서 그대로다.
> 결과 예시도 그때 실제로 나온 출력이다.

---

## 0. 매번 처음에 — 가상환경 켜기

**터미널을 새로 열 때마다 매번 해야 한다.** 창을 닫으면 풀린다.

```powershell
cd C:\Users\Samsung\source\전대용
.venv\Scripts\activate
```

프롬프트 맨 앞에 `(.venv)`가 붙으면 성공이다.

```
(.venv) PS C:\Users\Samsung\source\전대용>
 ↑ 이게 있어야 한다
```

**이걸 빼먹으면** `ModuleNotFoundError: No module named 'notion_client'` 가 난다.
코드가 고장난 게 아니다. 설치한 패키지는 `.venv` 폴더 안에만 있어서,
가상환경을 안 켜면 컴퓨터 기본 파이썬이 실행되고 그쪽엔 패키지가 없다.

---

## 1. 환경변수 점검

```powershell
python check_env.py
```

`.env` 파일의 키가 제대로 채워졌는지 본다. **키 값은 화면에 찍히지 않으므로**
결과를 캡처해서 팀 채널에 올려도 안전하다.

Notion에 실제로 접속되는지까지 확인하려면:

```powershell
python check_env.py --notion
```

성공하면 하위 페이지 목록이 나온다.

```
[O] Notion 접속 성공. 하위 페이지 6건 발견.
    - 프로젝트 문제정의
    - 운영비용
    ...
```

Notion 주소에서 페이지 ID만 뽑고 싶을 때:

```powershell
python check_env.py --id "https://www.notion.so/..."
```

주소는 **반드시 큰따옴표로 감싼다.** 안 그러면 `?` 뒤가 잘린다.

---

## 2. 벡터 DB 만들기 (수집 → 청킹 → 임베딩 → 저장)

```powershell
python build_db.py
```

Notion 문서를 가져와 300자 조각으로 자르고, 숫자 벡터로 바꿔 ChromaDB에 넣는다.
**Claude API 키 없이 동작한다.** 임베딩은 내 컴퓨터에서 계산되기 때문이다.

```
[1/3] Notion에서 문서를 수집합니다...
      문서 7건 수집
[2/3] 문서를 조각으로 자릅니다 (300자 / 겹침 50자)...
      조각 20개 생성
[3/3] 임베딩 후 벡터 DB에 저장합니다...
완료. 벡터 DB에 조각 20개가 들어 있습니다.
```

처음 실행하면 임베딩 모델(약 500MB)을 내려받느라 오래 걸린다. 그 뒤로는 캐시에서 읽는다.

| 옵션 | 언제 쓰나 |
|---|---|
| `--limit 3` | 3건만 빠르게 확인하고 싶을 때 |
| `--reset` | 기존 데이터를 **비우고** 새로 넣을 때 |

**Notion 문서를 고치거나 지웠으면 `--reset`을 쓴다.**
그냥 `build_db.py`만 돌리면 새 문서는 들어가지만 지워진 문서의 흔적이 남는다.

```powershell
python build_db.py --reset
```

---

## 3. 의미 검색 확인 (Claude 키 없이 됨)

```powershell
python -m app.services.qa_pipeline --search "회의는 언제 하나요"
```

질문과 의미가 가까운 조각을 유사도 순으로 보여준다.

```
질문: 회의는 언제 하나요
찾은 조각: 5건

  유사도 0.541  [회의 내용]
    회의 내용 1주차 회의 내용 2주차 회의 예정 아젠다...
    출처: https://app.notion.com/p/ad89b6a1...

  유사도 0.502  [회의 사진]
  유사도 0.442  [운영비용]
  유사도 0.316  [진행 사항 자료(기능정의서)]
  유사도 0.308  [프로젝트 문제정의]
```

### 유사도 읽는 법

| 유사도 | 뜻 |
|---|---|
| 0.5 이상 | 관련 있음 |
| 0.4 ~ 0.5 | 애매함 |
| 0.4 미만 | 사실상 무관 |

**벡터 검색은 "관련 없음"을 모른다.** 무조건 가장 가까운 것 5개를 돌려준다.
그래서 없는 내용을 물으면 0.4 미만짜리 엉뚱한 문서가 1위로 나온다.
고장난 게 아니라 그 내용이 문서에 없다는 뜻이다.

**질문은 적재된 문서에 실제로 있는 내용으로 해야 한다.**
어떤 문서가 들어있는지는 `build_db.py` 실행 끝에 목록으로 나온다.

---

## 4. 출처가 붙은 답변 받기 (Claude 키 필요)

`--search`를 **빼면** 검색 결과를 Claude에게 넘겨 문장으로 답한다.

```powershell
python -m app.services.qa_pipeline "회의는 언제 하나요"
```

```
질문: 회의는 언제 하나요

2주차와 3주차 회의가 예정되어 있습니다[1].

출처:
  [1] 회의 내용 — https://app.notion.com/p/ad89b6a1...
```

근거를 못 찾으면 지어내지 않고 `관련 문서를 찾지 못했습니다.`라고 답한다.
이건 정상 동작이다. 시스템 프롬프트에 그렇게 지시해 두었다.

---

## 4-1. 대화형 모드 — 질문을 여러 번 할 때

```powershell
python -m app.services.qa_pipeline --chat
```

```
임베딩 모델을 준비합니다. 처음 한 번만 걸립니다...

벡터 DB에 조각 31개가 있습니다.

질문을 입력하세요. 끝내려면 exit 또는 Ctrl+C.
질문 앞에 ? 를 붙이면 검색 결과만 봅니다 (Claude를 부르지 않아 무료).

질문> 카카오워크 담당은 누구야
카카오워크 담당은 임혜량(팀장)과 마준서입니다. [2]
...
질문> ?대면회의          ← ? 를 붙이면 검색 결과만. 돈 안 나간다
질문> exit
```

**왜 쓰나**: 위 3·4번 방식은 질문 한 번마다 임베딩 모델(약 500MB)을 새로
읽어서 매번 10~20초씩 걸린다. 프로그램이 끝나면 메모리에 올린 모델도 같이
사라지기 때문이다. 대화형 모드는 켜둔 채로 질문을 받으므로 **첫 질문만 느리고
그 뒤로는 바로 나온다.** 질문 표현을 바꿔가며 검색 품질을 확인할 때 편하다.

| 입력 | 동작 |
|---|---|
| `질문` | 검색 + Claude 답변 (Claude 키 필요) |
| `?질문` | 검색 결과만 (키 불필요, 무료) |
| `exit` / `종료` / `Ctrl+C` | 끝내기 |

Claude 호출이 실패해도 세션은 안 끊긴다. 오류 메시지를 보여주고 다음 질문을 받는다.

---

## 5. 서버 띄우기 (터미널 2개 필요)

**터미널 1** — 서버. 켜두고 건드리지 않는다.

```powershell
uvicorn app.main:app --reload --port 8000
```

**터미널 2** — 다른 명령을 치는 곳. 가상환경을 여기서도 켜야 한다.

서버를 띄운 창은 계속 돌아가느라 명령을 못 받는다. 그래서 창이 두 개 필요하다.
서버를 끄려면 터미널 1에서 `Ctrl+C`.

브라우저에서 열 주소:

| 주소 | 내용 |
|---|---|
| http://localhost:8000/health | 상태 확인. `{"status":"ok"}` 가 나오면 정상 |
| http://localhost:8000/docs | 자동 생성된 API 문서. `Try it out` 으로 실행해볼 수 있다 |

`http://localhost:8000/` (슬래시만)은 **404가 정상이다.** 그 주소에는 아무것도 안 만들었다.

---

## 6. 테스트

```powershell
pytest -q
```

```
.........xxx...................                    [100%]
```

- `.` = 통과
- `x` = **예상된 실패(xfail)**. 아직 구현 안 한 부분이라 일부러 표시해둔 것이다. 에러가 아니다.
- `F` = 진짜 실패. 이건 고쳐야 한다.

어떤 게 xfail인지 이유까지 보려면:

```powershell
pytest -q -rxX
```

---

## 자주 나는 오류

| 증상 | 원인 | 해결 |
|---|---|---|
| `ModuleNotFoundError: No module named 'notion_client'` | 가상환경이 꺼져 있음 | `.venv\Scripts\activate` |
| `ModuleNotFoundError: No module named 'app'` | 프로젝트 루트가 아닌 곳에서 실행 | `cd C:\Users\Samsung\source\전대용` |
| `ValidationError: notion_api_key field required` | `.env` 파일이 없음 | `copy .env.example .env` 후 키 채우기 |
| Notion `404` / `403` | 대상 페이지가 Integration과 연결 안 됨 | Notion에서 페이지 열기 → 우측 상단 `⋯` → 연결 → Integration 추가 |
| Notion `401` | API 키가 틀림 | https://www.notion.so/profile/integrations 에서 시크릿 재확인 |
| Claude `404` | 모델 이름 오타 | `app/core/config.py`의 `anthropic_model` 확인 |
| Claude 인증 실패 | `ANTHROPIC_API_KEY`가 자리표시자 | `notepad .env`로 진짜 키 입력 |
| 검색 결과가 다 0.4 미만 | 그 내용이 Notion에 없음 | Notion에 문서를 채우고 `build_db.py --reset` |
| `찾은 조각: 0건` | 벡터 DB가 비어 있음 | `python build_db.py` 먼저 실행 |
| 포트 8000 사용 중 | 서버가 이미 떠 있음 | 기존 터미널에서 `Ctrl+C`, 또는 `--port 8001` |

---

## 전체 흐름 요약

```powershell
# 매번
cd C:\Users\Samsung\source\전대용
.venv\Scripts\activate

# Notion 문서가 바뀌었을 때만
python build_db.py --reset

# 평소에 쓰는 것
python -m app.services.qa_pipeline --search "질문"    # 검색만 (무료)
python -m app.services.qa_pipeline "질문"             # 답변까지 (Claude 호출)
python -m app.services.qa_pipeline --chat             # 대화형 (여러 번 물어볼 때)
```

```
Notion 문서
   │  build_db.py
   ▼
ChromaDB (조각 20개)
   │  qa_pipeline --search   ← 여기까지 Claude 키 불필요
   ▼
유사도 순 검색 결과
   │  qa_pipeline (--search 없이)
   ▼
출처 붙은 답변
```

---

## 참고 문서

| 문서 | 내용 |
|---|---|
| [SETUP.md](./SETUP.md) | 팀원 설치 안내 — 코드를 처음 받았을 때 따라 하는 순서 |
| [README.md](./README.md) | 프로젝트 소개, 처음 클론했을 때 할 일 |
| [CLAUDE.md](./CLAUDE.md) | 확정 아키텍처, 기술 스택, 코딩 규칙 |
| [TEAM_CHECKLIST.md](./TEAM_CHECKLIST.md) | 주차별 체크리스트, 역할 분담 |
