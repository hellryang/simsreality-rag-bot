# app/main.py 업데이트
main_code = '''from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import sqlite3

app = FastAPI()
templates = Jinja2Templates(directory="app/templates")

# 연간 토큰 한도 설정 (12,000,000 토큰)
ANNUAL_TOKEN_LIMIT = 12000000

def init_db():
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            user_id TEXT,
            message TEXT,
            mode TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            used_tokens INTEGER
        )
    """)
    conn.commit()
    conn.close()

init_db()

class ChatRequest(BaseModel):
    user_id: str
    message: str
    mode: Optional[str] = "USER"

@app.get("/admin", response_class=HTMLResponse)
async def read_admin(request: Request):
    return templates.TemplateResponse(request=request, name="admin.html")

@app.get("/user", response_class=HTMLResponse)
@app.get("/user/{username}", response_class=HTMLResponse)
async def read_user(request: Request, username: Optional[str] = "임혜량"):
    return templates.TemplateResponse(request=request, name="user.html", context={"username": username})

@app.get("/api/tokens")
async def get_tokens():
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    
    cursor.execute("SELECT SUM(used_tokens), SUM(input_tokens), SUM(output_tokens), COUNT(*) FROM logs")
    row = cursor.fetchone()
    
    total_tok = row[0] or 0
    input_tok = row[1] or 0
    output_tok = row[2] or 0
    req_count = row[3] or 0
    
    cursor.execute("SELECT COUNT(*) FROM logs WHERE mode = 'SUMMARY'")
    summary_count = cursor.fetchone()[0] or 0
    
    cursor.execute("SELECT COUNT(*) FROM logs WHERE mode = 'NOTION'")
    notion_count = cursor.fetchone()[0] or 0
    
    conn.close()
    
    summary_percent = round((summary_count / req_count) * 100) if req_count > 0 else 0
    notion_percent = round((notion_count / req_count) * 100) if req_count > 0 else 0
    
    annual_tokens = total_tok
    remaining_annual_tokens = max(0, ANNUAL_TOKEN_LIMIT - annual_tokens)
    annual_usage_percentage = round((annual_tokens / ANNUAL_TOKEN_LIMIT) * 100, 1) if ANNUAL_TOKEN_LIMIT > 0 else 0
    
    return {
        "today_tokens": total_tok,
        "weekly_tokens": total_tok,
        "monthly_tokens": total_tok,
        "annual_tokens": annual_tokens,
        "annual_limit": ANNUAL_TOKEN_LIMIT,
        "remaining_annual_tokens": remaining_annual_tokens,
        "annual_usage_percentage": annual_usage_percentage,
        "total_requests": req_count,
        "input_tokens": input_tok,
        "output_tokens": output_tok,
        "daily_usage": [0, 0, 0, 0, 0, 0, total_tok],
        "summary_count": summary_count,
        "summary_percent": summary_percent,
        "notion_count": notion_count,
        "notion_percent": notion_percent
    }

@app.get("/api/user/{username}/tokens")
async def get_user_tokens(username: str):
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT SUM(used_tokens), SUM(input_tokens), SUM(output_tokens), COUNT(*) 
        FROM logs WHERE user_id = ?
    """, (username,))
    row = cursor.fetchone()
    conn.close()
    
    total_tok = row[0] or 0
    input_tok = row[1] or 0
    output_tok = row[2] or 0
    req_count = row[3] or 0
    
    return {
        "username": username,
        "today_tokens": total_tok,
        "weekly_tokens": total_tok,
        "monthly_tokens": total_tok,
        "total_requests": req_count,
        "input_tokens": input_tok,
        "output_tokens": output_tok,
        "daily_usage": [0, 0, 0, 0, 0, 0, total_tok]
    }

@app.get("/api/logs")
async def get_logs():
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT timestamp, user_id, message, mode, used_tokens FROM logs ORDER BY id DESC")
    rows = cursor.fetchall()
    conn.close()
    
    logs = [
        {
            "timestamp": r[0],
            "user_id": r[1],
            "message": r[2],
            "mode": r[3],
            "used_tokens": r[4]
        } for r in rows
    ]
    return {"logs": logs}

@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest):
    input_tok = len(req.message) * 2
    response_text = f"[{req.user_id}] 님의 요청 ('{req.message}')에 대한 카카오워크 AI 봇 응답입니다."
    output_tok = len(response_text) * 2
    total_tok = input_tok + output_tok
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO logs (timestamp, user_id, message, mode, input_tokens, output_tokens, used_tokens)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (timestamp, req.user_id, req.message, req.mode, input_tok, output_tok, total_tok))
    conn.commit()
    conn.close()

    return {
        "reply": response_text,
        "user_id": req.user_id,
        "used_tokens": total_tok
    }
'''

# app/templates/admin.html 업데이트
admin_code = '''<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <title>관리자 대시보드 - KakaoWork AI Bot</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        :root {
            --bg-dark: #0B0E14;
            --card-bg: #121824;
            --border-color: #1F293D;
            --text-main: #FFFFFF;
            --text-sub: #8A94A6;
            --accent-purple: #6366F1;
            --accent-cyan: #06B6D4;
            --accent-orange: #F59E0B;
            --accent-green: #10B981;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Pretendard', sans-serif; }
        body { background-color: var(--bg-dark); color: var(--text-main); padding: 20px 40px; }
        
        header { display: flex; justify-content: space-between; align-items: center; padding-bottom: 20px; border-bottom: 1px solid var(--border-color); }
        .logo { font-weight: bold; font-size: 16px; display: flex; align-items: center; gap: 8px; }
        .nav-btn { background: #1E293B; color: #94A3B8; padding: 6px 16px; border-radius: 20px; text-decoration: none; font-size: 13px; }
        .nav-btn.active { background: #4F46E5; color: #fff; }

        .title-area { margin: 24px 0; display: flex; justify-content: space-between; align-items: flex-end; }
        .title-area h1 { font-size: 22px; }
        .title-area p { color: var(--text-sub); font-size: 13px; margin-top: 4px; }
        .status-badge { color: #10B981; font-size: 12px; display: flex; align-items: center; gap: 6px; }

        .metrics-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 24px; }
        .card { background: var(--card-bg); border: 1px solid var(--border-color); border-radius: 12px; padding: 20px; }
        .card-label { color: var(--text-sub); font-size: 12px; margin-bottom: 12px; }
        .card-value { font-size: 26px; font-weight: bold; margin-bottom: 8px; }
        .card-sub { color: var(--text-sub); font-size: 11px; }

        .main-grid { display: grid; grid-template-columns: 2fr 1fr; gap: 20px; margin-bottom: 20px; }
        .chart-card { background: var(--card-bg); border: 1px solid var(--border-color); border-radius: 12px; padding: 20px; margin-bottom: 20px; }
        
        .side-card { background: var(--card-bg); border: 1px solid var(--border-color); border-radius: 12px; padding: 20px; margin-bottom: 20px; }
        .info-row { display: flex; justify-content: space-between; font-size: 13px; padding: 10px 0; border-bottom: 1px solid #1E293B; }

        .chart-footer-metrics { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-top: 16px; text-align: center; }
        .mini-metric-value { font-size: 18px; font-weight: bold; color: #818CF8; }
        .mini-metric-label { font-size: 11px; color: var(--text-sub); margin-top: 4px; }

        .category-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; margin-top: 12px; }
        .category-card { background: #161D2B; border: 1px solid var(--border-color); border-radius: 8px; padding: 16px; }
        .cat-percent { font-size: 24px; font-weight: bold; color: #818CF8; margin-bottom: 4px; }
        .cat-title { font-size: 13px; font-weight: bold; }
        .cat-sub { font-size: 11px; color: var(--text-sub); margin-top: 4px; }
        .progress-bar { height: 6px; background: #1E293B; border-radius: 3px; margin-top: 12px; overflow: hidden; }
        .progress-fill { height: 100%; background: #10B981; transition: width 0.3s ease; }

        .log-card { background: var(--card-bg); border: 1px solid var(--border-color); border-radius: 12px; padding: 20px; }
        table { width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 13px; }
        th, td { padding: 10px; text-align: left; border-bottom: 1px solid #1E293B; }
        th { color: var(--text-sub); font-weight: normal; }
    </style>
</head>
<body>
    <header>
        <div class="logo">⚡ KakaoWork AI Bot <span style="color:#64748B; font-size:12px;">SIMSREALITY</span></div>
        <div>
            <a href="/admin" class="nav-btn active">관리자</a>
            <a href="/user" class="nav-btn">사용자</a>
        </div>
    </header>

    <div class="title-area">
        <div>
            <h1>관리자 대시보드</h1>
            <p>카카오워크 AI 봇 · 실시간 요청 및 연간 토큰 현황</p>
        </div>
        <div class="status-badge">● 실시간 연동 중</div>
    </div>

    <!-- 상단 지표 카드 (연간 토큰 사용량으로 변경) -->
    <div class="metrics-grid">
        <div class="card" style="border-left: 4px solid var(--accent-purple);">
            <div class="card-label">연간 토큰 사용량</div>
            <div class="card-value" id="annual-tokens" style="color: #818CF8;">0</div>
            <div class="card-sub" id="annual-sub">연간 한도: 12,000,000 토큰 중 0.0% 사용</div>
        </div>
        <div class="card">
            <div class="card-label">금일 토큰 사용량</div>
            <div class="card-value" id="today-tokens">0</div>
            <div class="card-sub">오늘 소모한 토큰</div>
        </div>
        <div class="card">
            <div class="card-label">월간 토큰 사용량</div>
            <div class="card-value" id="monthly-tokens">0</div>
            <div class="card-sub">이번 달 소모 총 토큰</div>
        </div>
        <div class="card">
            <div class="card-label">총 요청 건수</div>
            <div class="card-value" id="total-requests">0건</div>
            <div class="card-sub">전체 누적 요청</div>
        </div>
    </div>

    <div class="main-grid">
        <div>
            <div class="chart-card">
                <h3 style="font-size: 15px; margin-bottom: 16px;">일별 토큰 사용 추이</h3>
                <canvas id="usageChart" height="110"></canvas>
                
                <div class="chart-footer-metrics">
                    <div>
                        <div class="mini-metric-value" id="footer-req">0건</div>
                        <div class="mini-metric-label">총 요청</div>
                    </div>
                    <div>
                        <div class="mini-metric-value" id="footer-tok">0</div>
                        <div class="mini-metric-label">총 토큰</div>
                    </div>
                    <div>
                        <div class="mini-metric-value" id="footer-in" style="color:#06B6D4;">0</div>
                        <div class="mini-metric-label">Input 토큰</div>
                    </div>
                    <div>
                        <div class="mini-metric-value" id="footer-out" style="color:#F59E0B;">0</div>
                        <div class="mini-metric-label">Output 토큰</div>
                    </div>
                </div>
            </div>

            <div class="chart-card">
                <h3 style="font-size: 15px; margin-bottom: 12px;">유형별 질의 분포</h3>
                <div class="category-grid">
                    <div class="category-card">
                        <div class="cat-percent" id="summary-percent" style="color:#818CF8;">0%</div>
                        <div class="cat-title">메신저 요약</div>
                        <div class="cat-sub" id="summary-count">0건 (이번 주)</div>
                        <div class="progress-bar"><div class="progress-fill" id="summary-bar" style="width: 0%; background: #818CF8;"></div></div>
                    </div>
                    <div class="category-card">
                        <div class="cat-percent" id="notion-percent" style="color:#06B6D4;">0%</div>
                        <div class="cat-title">노션 연동</div>
                        <div class="cat-sub" id="notion-count">0건 (이번 주)</div>
                        <div class="progress-bar"><div class="progress-fill" id="notion-bar" style="width: 0%; background: #06B6D4;"></div></div>
                    </div>
                </div>
            </div>
        </div>

        <div>
            <div class="side-card">
                <h3 style="font-size: 15px; margin-bottom: 12px;">Claude API 연간 한도</h3>
                <div class="info-row"><span>연간 한도량</span><span id="side-limit" style="font-weight:bold;">12,000,000</span></div>
                <div class="info-row"><span>연간 사용 토큰</span><span id="side-used" style="color:#F59E0B; font-weight:bold;">0</span></div>
                <div class="info-row"><span>연간 잔여 토큰</span><span id="side-rem" style="color:#10B981; font-weight:bold;">12,000,000</span></div>
                <div class="progress-bar" style="margin-top:14px;">
                    <div class="progress-fill" id="limit-bar" style="width: 0%; background: #818CF8;"></div>
                </div>
            </div>

            <div class="side-card">
                <h3 style="font-size: 15px; margin-bottom: 12px;">Claude API 토큰 구성</h3>
                <div class="info-row"><span>Input 토큰</span><span id="side-input" style="color:#818CF8; font-weight:bold;">0</span></div>
                <div class="info-row"><span>Output 토큰</span><span id="side-output" style="color:#06B6D4; font-weight:bold;">0</span></div>
                <div class="info-row"><span>합계 소모 토큰</span><span id="side-total" style="color:#F59E0B; font-weight:bold;">0</span></div>
            </div>
        </div>
    </div>

    <div class="log-card">
        <h3 style="font-size: 15px;">사용자 로그 저장소 (실시간 요청 내역)</h3>
        <table>
            <thead>
                <tr>
                    <th>시간</th>
                    <th>사용자 ID</th>
                    <th>요청 메시지</th>
                    <th>모드</th>
                    <th>소모 토큰</th>
                </tr>
            </thead>
            <tbody id="log-table-body">
                <tr>
                    <td colspan="5" style="text-align:center; color: var(--text-sub);">저장된 요청 로그가 없습니다.</td>
                </tr>
            </tbody>
        </table>
    </div>

    <script>
        let chartInstance = null;

        async function fetchData() {
            try {
                const tokenRes = await fetch('/api/tokens');
                const data = await tokenRes.json();

                const annualTokens = data.annual_tokens || 0;
                const annualLimit = data.annual_limit || 12000000;
                const usagePercent = data.annual_usage_percentage || 0;

                document.getElementById('annual-tokens').innerText = annualTokens.toLocaleString();
                document.getElementById('annual-sub').innerText = `연간 한도: ${annualLimit.toLocaleString()} 토큰 중 ${usagePercent}% 사용`;

                document.getElementById('today-tokens').innerText = (data.today_tokens || 0).toLocaleString();
                document.getElementById('monthly-tokens').innerText = (data.monthly_tokens || 0).toLocaleString();
                document.getElementById('total-requests').innerText = (data.total_requests || 0) + "건";

                document.getElementById('footer-req').innerText = (data.total_requests || 0) + "건";
                document.getElementById('footer-tok').innerText = (data.today_tokens || 0).toLocaleString();
                document.getElementById('footer-in').innerText = (data.input_tokens || 0).toLocaleString();
                document.getElementById('footer-out').innerText = (data.output_tokens || 0).toLocaleString();

                document.getElementById('side-limit').innerText = annualLimit.toLocaleString();
                document.getElementById('side-used').innerText = annualTokens.toLocaleString();
                document.getElementById('side-rem').innerText = (data.remaining_annual_tokens || 0).toLocaleString();
                document.getElementById('limit-bar').style.width = usagePercent + "%";

                document.getElementById('side-input').innerText = (data.input_tokens || 0).toLocaleString();
                document.getElementById('side-output').innerText = (data.output_tokens || 0).toLocaleString();
                document.getElementById('side-total').innerText = (data.today_tokens || 0).toLocaleString();

                document.getElementById('summary-percent').innerText = (data.summary_percent || 0) + "%";
                document.getElementById('summary-count').innerText = (data.summary_count || 0) + "건 (이번 주)";
                document.getElementById('summary-bar').style.width = (data.summary_percent || 0) + "%";

                document.getElementById('notion-percent').innerText = (data.notion_percent || 0) + "%";
                document.getElementById('notion-count').innerText = (data.notion_count || 0) + "건 (이번 주)";
                document.getElementById('notion-bar').style.width = (data.notion_percent || 0) + "%";

                if (chartInstance && data.daily_usage) {
                    chartInstance.data.datasets[0].data = data.daily_usage;
                    chartInstance.update();
                }

                const logRes = await fetch('/api/logs');
                const logData = await logRes.json();
                const tbody = document.getElementById('log-table-body');

                if (logData.logs && logData.logs.length > 0) {
                    tbody.innerHTML = logData.logs.map(log => `
                        <tr>
                            <td>${log.timestamp}</td>
                            <td><b style="color:#818CF8;">${log.user_id}</b></td>
                            <td>${log.message}</td>
                            <td><span style="background:#1E293B; padding:2px 8px; border-radius:4px; font-size:11px;">${log.mode}</span></td>
                            <td style="color:#F59E0B; font-weight:bold;">${log.used_tokens}</td>
                        </tr>
                    `).join('');
                } else {
                    tbody.innerHTML = '<tr><td colspan="5" style="text-align:center; color: var(--text-sub);">저장된 요청 로그가 없습니다.</td></tr>';
                }
            } catch (err) {
                console.error("데이터 동기화 실패:", err);
            }
        }

        const ctx = document.getElementById('usageChart').getContext('2d');
        chartInstance = new Chart(ctx, {
            type: 'line',
            data: {
                labels: ['D-6', 'D-5', 'D-4', 'D-3', 'D-2', '어제', '오늘'],
                datasets: [{
                    label: '토큰 사용량',
                    data: [0, 0, 0, 0, 0, 0, 0],
                    borderColor: '#6366F1',
                    backgroundColor: 'rgba(99, 102, 241, 0.15)',
                    fill: true,
                    tension: 0.3
                }]
            },
            options: {
                plugins: { legend: { display: false } },
                scales: {
                    x: { grid: { color: '#1E293B' }, ticks: { color: '#64748B' } },
                    y: { grid: { color: '#1E293B' }, ticks: { color: '#64748B' } }
                }
            }
        });

        fetchData();
        setInterval(fetchData, 3000);
    </script>
</body>
</html>
'''

with open("app/main.py", "w", encoding="utf-8") as f:
    f.write(main_code)

with open("app/templates/admin.html", "w", encoding="utf-8") as f:
    f.write(admin_code)

print("SUCCESS: 모든 코드가 연간 토큰량 기준으로 업데이트되었습니다!")