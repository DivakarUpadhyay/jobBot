"""The list of freelance services every freelance email shows as bullet points (user, 2026-10-16:
"in freelance add different kinds of service in bullet points ... software development, testings, software design,
mobile app development and testings and IoT projects and many more"). Narrowed 2026-10-19 (user: "don't offer IoT",
"just mobile and web development", "payment gateway and third party integrations", "maintenance and new features enhancement in existing systems"). One place, so all freelance emails match."""

SERVICES = [
    ("網站與網頁系統開發：企業網站、後台管理系統、會員與電商平台（C#／.NET、React、Next.js、Angular）",
     "Web development: company websites, web applications, admin back offices, member and e-commerce platforms (C# / .NET, React, Next.js, Angular)"),
    ("網站後端與 API 開發，含資料庫設計（SQL Server）",
     "Web back ends and APIs, including the database (SQL Server)"),
    ("行動 App 開發：Android 與 iOS（跨平台）",
     "Mobile app development: Android and iOS (cross-platform)"),
    ("金流串接（綠界 ECPay、藍新 NewebPay、LINE Pay、Stripe 等）與第三方 API 整合：物流、電子發票、CRM、ERP、簡訊",
     "Payment gateway integration (Stripe, PayPal, Razorpay, ECPay and others) and third-party APIs: shipping, CRM, ERP, SMS"),
    ("既有系統、網站與 App 的維護：錯誤修正、新功能開發與功能優化、升級、改版與上線部署",
     "Maintenance of existing systems, websites and apps: bug fixes, new features and enhancements, upgrades, redesign and deployment"),
]


def bullets_en() -> str:
    return "\n".join(f"- {en}" for _, en in SERVICES)


def bullets_zh() -> str:
    return "\n".join(f"・{zh}" for zh, _ in SERVICES)
