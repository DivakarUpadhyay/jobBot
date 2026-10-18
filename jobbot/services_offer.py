"""The list of freelance services every freelance email shows as bullet points (user, 2026-10-16:
"in freelance add different kinds of service in bullet points ... software development, testings, software design,
mobile app development and testings and IoT projects and many more"). One place, so all freelance emails match."""

SERVICES = [
    ("客製化軟體開發：網站系統、後台、API 與企業應用（C#／.NET）",
     "Custom software development: web applications, back offices, APIs and business systems (C# / .NET)"),
    ("軟體設計與架構：系統設計、資料庫設計、技術文件",
     "Software design and architecture: system design, database design, technical documentation"),
    ("軟體測試與品質保證：功能測試、自動化測試、API 測試、效能測試",
     "Software testing and QA: functional testing, automated tests, API testing, performance testing"),
    ("行動 App 開發與測試（Android／iOS，跨平台）",
     "Mobile app development and testing (Android / iOS, cross-platform)"),
    ("網頁前端與管理儀表板（Next.js、React、Angular）",
     "Web front ends and admin dashboards (Next.js, React, Angular)"),
    ("IoT 物聯網專案：感測器資料收集、設備監控儀表板、MQTT／OPC UA 串接、智慧工廠監控",
     "IoT projects: sensor data collection, device monitoring dashboards, MQTT / OPC UA integration, smart factory monitoring"),
    ("資料庫與報表：SQL Server 設計與效能調校、SSRS 報表、SSIS 資料流程",
     "Databases and reporting: SQL Server design and tuning, SSRS reports, SSIS data pipelines"),
    ("系統整合：金流、物流、ERP 與第三方 API 串接",
     "System integration: payments, logistics, ERP and third-party APIs"),
    ("既有系統維護、升級與雲端部署（Azure、CI/CD）",
     "Maintenance and modernisation of existing applications, cloud deployment (Azure, CI/CD)"),
    ("AI 與自動化：聊天機器人、文件處理、流程自動化",
     "AI and automation: chatbots, document processing, workflow automation"),
]


def bullets_en() -> str:
    return "\n".join(f"- {en}" for _, en in SERVICES)


def bullets_zh() -> str:
    return "\n".join(f"・{zh}" for zh, _ in SERVICES)
