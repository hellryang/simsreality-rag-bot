import os
import httpx
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(dotenv_path=BASE_DIR / ".env")

# NOTION_TOKEN 또는 NOTION_API_KEY 로드
NOTION_TOKEN = (os.getenv("NOTION_TOKEN") or os.getenv("NOTION_API_KEY") or "").strip()
NOTION_ROOT_PAGE_ID = (os.getenv("NOTION_ROOT_PAGE_ID") or os.getenv("NOTION_DATABASE_ID") or "").strip()

async def fetch_notion_schedules(mode: str):
    if not NOTION_TOKEN or not NOTION_ROOT_PAGE_ID:
        return f"❌ [.env 설정 오류] API 토큰 또는 페이지 ID가 비어있습니다."

    # 페이지 내부의 블록(글, 리스트 등)을 읽어오는 엔드포인트
    url = f"https://api.notion.com/v1/blocks/{NOTION_ROOT_PAGE_ID}/children"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json"
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers)
        
        if response.status_code == 200:
            data = response.json()
            results = data.get("results", [])

            if not results:
                return "📌 노션 페이지 연결 성공! (단, 페이지 내부에 작성된 블록/글이 없습니다.)"

            formatted_text = f"📌 [{mode} 모드] 노션 실시간 연동 페이지 내용\n\n"
            
            lines = []
            for block in results:
                block_type = block.get("type")
                if not block_type:
                    continue
                
                # 텍스트가 들어있는 주요 블록 파싱 (paragraph, bulleted_list_item, numbered_list_item, heading_1 등)
                block_data = block.get(block_type, {})
                rich_text = block_data.get("rich_text", [])
                
                if rich_text:
                    text_content = "".join([t.get("plain_text", "") for t in rich_text])
                    if text_content.strip():
                        lines.append(f"• {text_content}")

            if lines:
                formatted_text += "\n".join(lines)
            else:
                formatted_text += "페이지에서 텍스트 내용을 찾지 못했습니다."

            return formatted_text

        else:
            return (
                f"❌ [노션 API 연결 실패 - HTTP {response.status_code}]\n\n"
                f"응답 내용: {response.text}\n"
            )

    except Exception as e:
        return f"❌ [시스템 예외 발생]: {str(e)}"