import os
from notion_client import Client

# .env 파일이나 환경변수에서 노션 API 토큰 및 DB ID 로드
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "your_notion_integration_token_here")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID", "your_database_id_here")

notion = Client(auth=NOTION_TOKEN)

async def fetch_notion_schedules(mode: str):
    try:
        # 노션 데이터베이스 쿼리 실행
        response = notion.databases.query(database_id=NOTION_DATABASE_ID)
        results = response.get("results", [])

        if not results:
            return "노션 데이터베이스에 등록된 회의 일정이 없습니다."

        formatted_text = f"📌 [{mode} 모드] 노션 실시간 연동 회의 일정\n\n"
        
        for page in results:
            props = page.get("properties", {})
            
            # 노션 DB 속성 명칭에 맞게 데이터 추출 (예: 날짜, 작성자, 대화방, 내용)
            title = props.get("내용", {}).get("title", [{}])[0].get("plain_text", "제목 없음")
            date = props.get("날짜", {}).get("date", {}).get("start", "날짜 미정")
            author = props.get("작성자", {}).get("rich_text", [{}])[0].get("plain_text", "미상")
            room = props.get("대화방", {}).get("select", {}).get("name", "일반")

            formatted_text += (
                f"--------------------------------------------------\n"
                f"📅 날짜: {date}\n"
                f"👤 작성자: {author}\n"
                f"💬 대화방: {room}\n"
                f"📝 내용: {title}\n"
            )

        return formatted_text

    except Exception as e:
        return f"노션 API 연동 중 오류 발생: {str(e)}\n(.env 파일의 NOTION_TOKEN 및 DATABASE_ID를 확인해 주세요.)"