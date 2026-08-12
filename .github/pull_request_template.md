## 무엇을 했나

<!-- 한두 줄로. 예: Notion 페이지 본문 수집 + 제목 추출 구현 -->

## 리뷰어가 봐줬으면 하는 곳

<!-- 자신 없는 부분을 솔직히 적으면 리뷰가 빨라진다. 없으면 "없음" -->

## 체크리스트

- [ ] `pytest -q` 가 로컬에서 통과한다
- [ ] 계약 테스트를 통과시켰다면 `@pytest.mark.xfail` 한 줄을 지웠다
- [ ] API 키를 코드에 문자열로 넣지 않았다 (`.env`만 사용)
- [ ] `git status`에 `.env`나 `chroma_data/`가 뜨지 않는다
- [ ] 외부 API 호출에 `try/except` + 재시도를 넣었다
- [ ] 벡터에 넣는 조각에 `source` / `url` / `title` / `created_at`을 붙였다

## 공용 파일을 건드렸나

- [ ] `app/models/schemas.py` — 건드렸다면 **팀 채널에 공유 필수** (전원 영향)
- [ ] `app/services/vector_store.py` / `qa_pipeline.py` — 위와 동일
