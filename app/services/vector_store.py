"""벡터 스토어 래퍼.

현재는 ChromaDB(cosine)를 사용한다. 4~5주차에 PostgreSQL + pgvector(Supabase)로
교체할 예정이므로, 호출부는 이 모듈의 인터페이스에만 의존하도록 유지한다.
"""
