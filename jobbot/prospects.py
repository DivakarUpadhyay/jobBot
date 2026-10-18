"""Speculative applications to Taiwan companies, chosen by city, district and industry. One email per
company offers full-time, part-time and freelance / project work together (never three separate emails).

The companies are the ones listed on the Taiwan Stock Exchange and the Taipei Exchange (table
`companies`). Their official records give the address, the industry and the website. From those:

  refresh   reads both exchange lists again and stores address -> city and district, industry name,
            website and the contact address the company publishes in its record
  find      reads each company's own website (home, careers / recruiting / contact pages) for a
            hiring address on the company's domain; the domain must accept mail
  queue     writes one bilingual email per company (Traditional Chinese first, then English),
            with the resume attached, into `jobs` as job_type 'prospect'

Sending goes through the normal queue (sendqueue): one recipient, warm-up caps, office hours, never
the same address twice, never a company written to in the last N days, and at most
`prospects.per_day_per_account` of these per account per day, after the real job applications.

Approved automatically: an address found on the company's own domain (hiring address first, else its
general inbox) and a general inbox (info@, service@ ...) given in the exchange record. Any other
exchange-record address is usually investor relations, finance or the spokesperson; those emails are
written but wait for you (`jobbot prospects approve --published`)."""
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, TimeoutError as Timeout
from urllib.parse import urljoin, urlparse

import requests

from .services_offer import bullets_en, bullets_zh
from . import contacts as ct, db
from .filter import EMAIL_RE
from .sources.base import UA

# ---------------------------------------------------------------- places

CITY_EN = {"台北市": "Taipei", "新北市": "New Taipei", "基隆市": "Keelung", "桃園市": "Taoyuan", "新竹市": "Hsinchu City",
           "新竹縣": "Hsinchu County", "苗栗縣": "Miaoli", "台中市": "Taichung", "彰化縣": "Changhua", "南投縣": "Nantou",
           "雲林縣": "Yunlin", "嘉義市": "Chiayi City", "嘉義縣": "Chiayi County", "台南市": "Tainan", "高雄市": "Kaohsiung",
           "屏東縣": "Pingtung", "宜蘭縣": "Yilan", "花蓮縣": "Hualien", "台東縣": "Taitung", "澎湖縣": "Penghu",
           "金門縣": "Kinmen", "連江縣": "Lienchiang"}
CITY_ALIASES = {"taipei": "台北市", "taipeicity": "台北市", "newtaipei": "新北市", "newtaipeicity": "新北市",
                "keelung": "基隆市", "taoyuan": "桃園市", "hsinchu": "新竹", "hsinchucity": "新竹市",
                "hsinchucounty": "新竹縣", "miaoli": "苗栗縣", "taichung": "台中市", "changhua": "彰化縣", "nantou": "南投縣",
                "yunlin": "雲林縣", "chiayi": "嘉義", "tainan": "台南市", "kaohsiung": "高雄市", "pingtung": "屏東縣",
                "yilan": "宜蘭縣", "hualien": "花蓮縣", "taitung": "台東縣", "penghu": "澎湖縣", "kinmen": "金門縣"}

# First three digits of the postal code -> (city, district). Used when the address names no district
# (common in the exchange records, e.g. "台北市中山北路2段113號" with "Taipei City 104" in English).
_POSTAL = {
    "台北市": "100中正 103大同 104中山 105松山 106大安 108萬華 110信義 111士林 112北投 114內湖 115南港 116文山",
    "基隆市": "200仁愛 201信義 202中正 203中山 204安樂 205暖暖 206七堵",
    "新北市": "207萬里 208金山 220板橋 221汐止 222深坑 223石碇 224瑞芳 226平溪 227雙溪 228貢寮 231新店 232坪林 "
             "233烏來 234永和 235中和 236土城 237三峽 238樹林 239鶯歌 241三重 242新莊 243泰山 244林口 247蘆洲 "
             "248五股 249八里 251淡水 252三芝 253石門",
    "宜蘭縣": "260宜蘭 261頭城 262礁溪 263壯圍 264員山 265羅東 266三星 267大同 268五結 269冬山 270蘇澳 272南澳",
    "新竹縣": "302竹北 303湖口 304新豐 305新埔 306關西 307芎林 308寶山 310竹東 311五峰 312橫山 313尖石 314北埔 315峨眉",
    "桃園市": "320中壢 324平鎮 325龍潭 326楊梅 327新屋 328觀音 330桃園 333龜山 334八德 335大溪 336復興 337大園 338蘆竹",
    "苗栗縣": "350竹南 351頭份 352三灣 353南庄 354獅潭 356後龍 357通霄 358苑裡 360苗栗 361造橋 362頭屋 363公館 "
             "364大湖 365泰安 366銅鑼 367三義 368西湖 369卓蘭",
    "台中市": "400中區 401東區 402南區 403西區 404北區 406北屯 407西屯 408南屯 411太平 412大里 413霧峰 414烏日 "
             "420豐原 421后里 422石岡 423東勢 424和平 426新社 427潭子 428大雅 429神岡 432大肚 433沙鹿 434龍井 "
             "435梧棲 436清水 437大甲 438外埔 439大安",
    "彰化縣": "500彰化 502芬園 503花壇 504秀水 505鹿港 506福興 507線西 508和美 509伸港 510員林 511社頭 512永靖 "
             "513埔心 514溪湖 515大村 516埔鹽 520田中 521北斗 522田尾 523埤頭 524溪州 525竹塘 526二林 527大城 "
             "528芳苑 530二水",
    "南投縣": "540南投 541中寮 542草屯 544國姓 545埔里 546仁愛 551名間 552集集 553水里 555魚池 556信義 557竹山 558鹿谷",
    "雲林縣": "630斗南 631大埤 632虎尾 633土庫 634褒忠 635東勢 636臺西 637崙背 638麥寮 640斗六 643林內 646古坑 "
             "647莿桐 648西螺 649二崙 651北港 652水林 653口湖 654四湖 655元長",
    "台南市": "700中西 701東區 702南區 704北區 708安平 709安南 710永康 711歸仁 712新化 713左鎮 714玉井 715楠西 "
             "716南化 717仁德 718關廟 719龍崎 720官田 721麻豆 722佳里 723西港 724七股 725將軍 726學甲 727北門 "
             "730新營 731後壁 732白河 733東山 734六甲 735下營 736柳營 737鹽水 741善化 742大內 743山上 744新市 745安定",
    "高雄市": "800新興 801前金 802苓雅 803鹽埕 804鼓山 805旗津 806前鎮 807三民 811楠梓 812小港 813左營 814仁武 "
             "815大社 820岡山 821路竹 822阿蓮 823田寮 824燕巢 825橋頭 826梓官 827彌陀 828永安 829湖內 830鳳山 "
             "831大寮 832林園 833鳥松 840大樹 842旗山 843美濃 844六龜 845內門 846杉林 847甲仙 848桃源 849那瑪夏 "
             "851茂林 852茄萣",
    "屏東縣": "900屏東 901三地門 902霧台 903瑪家 904九如 905里港 906高樹 907鹽埔 908長治 909麟洛 911竹田 912內埔 "
             "913萬丹 920潮州 921泰武 922來義 923萬巒 924崁頂 925新埤 926南州 927林邊 928東港 929琉球 931佳冬 "
             "932新園 940枋寮 941枋山 942春日 943獅子 944車城 945牡丹 946恆春 947滿州",
}
POSTAL = {code[:3]: (city, code[3:]) for city, codes in _POSTAL.items() for code in codes.split()}
for _code in ("300",):
    POSTAL[_code] = ("新竹市", "")          # the whole of Hsinchu City shares 300
for _code, _dist in (("600", ""), ("602", "番路"), ("603", "梅山"), ("604", "竹崎"), ("605", "阿里山"), ("606", "中埔"),
                     ("607", "大埔"), ("608", "水上"), ("611", "鹿草"), ("612", "太保"), ("613", "朴子"), ("614", "東石"),
                     ("615", "六腳"), ("616", "新港"), ("621", "民雄"), ("622", "大林"), ("623", "溪口"), ("624", "義竹"),
                     ("625", "布袋")):
    POSTAL[_code] = ("嘉義市" if _code == "600" else "嘉義縣", _dist)

_towns = Counter(d for _, d in POSTAL.values() if d)
TOWN_CITY = {d: c for c, d in POSTAL.values() if d and _towns[d] == 1 and c.endswith("縣")}   # county towns only
CITY_RE = re.compile("(" + "|".join(CITY_EN) + ")")
DISTRICT_RE = re.compile(r"^(.{1,3}?(?:區|市|鎮|鄉))")


def place(address_zh, address_en=""):
    """(city, district) from the Chinese and English addresses of a company record. Either may be ''."""
    zh = (address_zh or "").replace("臺", "台").replace("巿", "市").replace(" ", "")
    city = district = ""
    if zh.startswith("新竹科學") and not CITY_RE.search(zh):
        return "新竹市", "科學園區"      # Hsinchu Science Park addresses often name no city
    town = re.match(r"^(?:\d{3,6})?(.{2,3}?)(?:市|鎮|鄉)", zh)
    if town and not CITY_RE.search(zh[:4]) and town.group(1) in TOWN_CITY:   # "彰化市中山路..." -> 彰化縣 彰化
        return TOWN_CITY[town.group(1)], town.group(1)
    m = CITY_RE.search(zh)
    if m:
        city = m.group(1)
        rest = zh[m.end():]
        d = DISTRICT_RE.match(rest)
        if d and not re.match(r"^.{0,3}[路街道段號]", d.group(1)):
            district = re.sub(r"(區|市|鎮|鄉)$", "", d.group(1))
    if not district:
        for code in re.findall(r"(?<!\d)(\d{3})(?:\d{2,3})?(?!\d)", f"{zh} {address_en or ''}"):
            if code in POSTAL and (not city or POSTAL[code][0] == city or city.startswith(POSTAL[code][0][:2])):
                city, district = city or POSTAL[code][0], POSTAL[code][1]
                break
    if not city:   # English only, e.g. "...Zhongzheng Dist.,Taipei City 100028"
        en = re.sub(r"[^a-z]", "", (address_en or "").lower())
        for alias, zh_city in sorted(CITY_ALIASES.items(), key=lambda x: -len(x[0])):
            if alias in en and zh_city in CITY_EN:
                city = zh_city
                break
    return city, district


def city_matches(city, wanted):
    """'Taipei', '台北', '臺北市', 'hsinchu' (city and county) all work."""
    w = (wanted or "").strip().replace("臺", "台")
    if not w:
        return True
    zh = CITY_ALIASES.get(re.sub(r"[^a-z]", "", w.lower()), w)
    return bool(city) and city.startswith(zh.rstrip("市縣"))


# ---------------------------------------------------------------- industries

# Exchange industry code -> (Chinese, English, fit tier). Tier 1 = software is the business or the
# product, 2 = large tech or regulated firms with their own IT departments, 3 = the rest.
INDUSTRY = {
    "01": ("水泥工業", "cement", 3), "02": ("食品工業", "food", 3), "03": ("塑膠工業", "plastics", 3),
    "04": ("紡織纖維", "textiles", 3), "05": ("電機機械", "electrical machinery", 2), "06": ("電器電纜", "electrical cables", 3),
    "08": ("玻璃陶瓷", "glass and ceramics", 3), "09": ("造紙工業", "paper", 3), "10": ("鋼鐵工業", "steel", 3),
    "11": ("橡膠工業", "rubber", 3), "12": ("汽車工業", "automotive", 2), "14": ("建材營造", "construction", 3),
    "15": ("航運業", "shipping and logistics", 2), "16": ("觀光餐旅", "tourism and hospitality", 3),
    "17": ("金融保險", "finance and insurance", 1), "18": ("貿易百貨", "trading and retail", 2), "19": ("綜合", "conglomerate", 3),
    "20": ("其他", "other industries", 3), "21": ("化學工業", "chemicals", 3), "22": ("生技醫療", "biotech and healthcare", 2),
    "23": ("油電燃氣", "energy and utilities", 3), "24": ("半導體", "semiconductors", 2),
    "25": ("電腦及週邊設備", "computers and peripherals", 1), "26": ("光電", "optoelectronics", 2),
    "27": ("通信網路", "telecommunications and networking", 1), "28": ("電子零組件", "electronic components", 2),
    "29": ("電子通路", "electronics distribution", 2), "30": ("資訊服務", "IT services", 1),
    "31": ("其他電子", "electronics", 2), "32": ("文化創意", "cultural and creative", 3), "33": ("農業科技", "agritech", 3),
    "34": ("電子商務", "e-commerce", 1), "35": ("綠能環保", "green energy", 3), "36": ("數位雲端", "digital and cloud services", 1),
    "37": ("運動休閒", "sports and leisure", 3), "38": ("居家生活", "home and living", 3), "80": ("管理股票", "managed stocks", 3),
    "91": ("存託憑證", "depositary receipts", 3),
}
INDUSTRY_ALIASES = {"it": "30", "software": "30", "itservices": "30", "cloud": "36", "digital": "36", "ecommerce": "34",
                    "finance": "17", "fintech": "17", "bank": "17", "insurance": "17", "telecom": "27", "network": "27",
                    "computer": "25", "hardware": "25", "semiconductor": "24", "semi": "24", "ic": "24",
                    "optoelectronics": "26", "electronics": "31", "components": "28", "distribution": "29",
                    "biotech": "22", "healthcare": "22", "medical": "22", "health": "22", "shipping": "15",
                    "logistics": "15", "retail": "18", "trading": "18", "automotive": "12", "machinery": "05"}

# What the email says the candidate could do for a company in that line of business. Only facts from
# the resume: Acer contact-service system, FedEx/TNT EDI and DHL, payment gateways, Sumsub KYC and
# audit trails, the Health Assure platform, SSRS/SSIS, Next.js portals.
ANGLE = {
    "it": ("我能直接投入貴公司為客戶開發與維護的 .NET 專案，包括後端 API、系統整合與資料庫設計，也習慣與客戶及專案經理直接溝通需求。",
           "I can contribute directly to the .NET projects you build and maintain for your clients, from backend APIs and "
           "system integrations to database design, and I am used to working with clients and project managers on requirements."),
    "finance": ("我曾整合 Stripe、PayU 等金流服務，並在金融科技平台實作 KYC 身分驗證、提領審核佇列與只增不改的稽核紀錄，熟悉金融系統對安全與合規的要求。",
                "I have integrated payment gateways such as Stripe and PayU, and built KYC verification, a withdrawal review "
                "queue and append-only audit trails on a FinTech platform, so I understand the security and compliance "
                "standards financial systems require."),
    "hardware": ("我曾派駐宏碁（Acer）維護及擴充其客服維修系統，該系統服務全臺維修中心與合作夥伴，並負責建置 SSRS 報表與 SSIS 自動化資料流程。",
                 "I worked on-site at Acer, maintaining and extending the Contact Service System used by its repair centers "
                 "and partners across Taiwan, and built its SSRS reporting suite and SSIS data pipelines."),
    "manufacturing": ("我能支援貴公司的內部企業系統，例如系統間的資料整合、SSRS 報表、SSIS 自動化資料流程，以及以 .NET 開發的內部管理平台。",
                      "I can support your internal enterprise systems, such as data integration between systems, SSRS "
                      "reporting, automated SSIS data pipelines and internal .NET business applications."),
    "logistics": ("我曾為宏碁整合 FedEx 與 TNT 的 EDI（多種文件格式）以及 DHL 付款閘道，熟悉物流與貨運相關的系統串接。",
                  "I have integrated FedEx and TNT EDI (several document formats) and the DHL payment gateway for Acer, so I know how "
                  "logistics and shipping systems connect."),
    "health": ("我曾在醫療健康平台 Health Assure 擔任資深工程師，參與企業與個人健康服務系統的開發，並整合 Apollo Pharmacy、Doc24x7 等外部服務。",
               "I worked as a Senior Software Engineer at Health Assure, a corporate and consumer health platform, and "
               "integrated external services such as Apollo Pharmacy and Doc24x7."),
    "commerce": ("我曾開發 Next.js 前台與 .NET 後端，並整合 Stripe、PayU 等金流，能支援貴公司電商、會員及訂單系統的開發。",
                 "I have built Next.js front ends on .NET backends and integrated payment gateways such as Stripe and PayU, "
                 "which fits work on e-commerce, membership and order systems."),
    "telecom": ("我擅長設計 REST 與 SOAP API 並串接第三方系統，能協助貴公司的後台服務、客戶入口網站與內部管理系統。",
                "I design REST and SOAP APIs and connect third-party systems, which fits work on your backend services, "
                "customer portals and internal management systems."),
    "general": ("我能協助貴公司開發與維護內部系統，例如以 .NET 開發的管理平台、SQL Server 資料庫、SSRS 報表與系統整合。",
                "I can help build and maintain your internal systems, such as .NET business applications, SQL Server "
                "databases, SSRS reporting and system integrations."),
}
ANGLE_OF = {"30": "it", "36": "it", "17": "finance", "25": "hardware", "15": "logistics", "22": "health", "34": "commerce",
            "18": "commerce", "27": "telecom", "24": "manufacturing", "26": "manufacturing", "28": "manufacturing",
            "29": "manufacturing", "31": "manufacturing", "05": "manufacturing", "12": "manufacturing"}


def industry_code(wanted):
    """'30', '資訊服務', 'IT', 'fintech' -> '30'. Unknown -> None."""
    w = (wanted or "").strip()
    if w in INDUSTRY:
        return w
    if w.isdigit() and w.zfill(2) in INDUSTRY:
        return w.zfill(2)
    for code, (zh, en, _) in INDUSTRY.items():
        if w and (w in zh or w.lower() == en.lower()):
            return code
    return INDUSTRY_ALIASES.get(re.sub(r"[^a-z]", "", w.lower()))


# ---------------------------------------------------------------- the company list

FIELDS = {   # exchange -> its field names
    "TWSE": dict(url="https://openapi.twse.com.tw/v1/opendata/t187ap03_L", zh="公司名稱", short="公司簡稱", en="英文簡稱",
                 web="網址", industry="產業別", addr="住址", addr_en="英文通訊地址", email="電子郵件信箱"),
    "TPEx": dict(url="https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O", zh="CompanyName", short="CompanyAbbreviation",
                 en="Symbol", web="WebAddress", industry="SecuritiesIndustryCode", addr="Address",
                 addr_en="StockTransferAgentAddress", email="EmailAddress"),
}
COLUMNS = {"short_name": "TEXT", "address": "TEXT", "city": "TEXT", "district": "TEXT", "published_email": "TEXT",
           "hr_email": "TEXT", "hr_email_where": "TEXT", "email_checked_at": "TEXT"}


def create(con):
    """Migration step: the columns this module keeps on `companies` (the caller commits)."""
    have = {r[1] for r in con.execute("PRAGMA table_info(companies)")}
    for name, kind in COLUMNS.items():
        if name not in have:
            con.execute(f"ALTER TABLE companies ADD COLUMN {name} {kind}")


def _clean(v):
    return re.sub(r"[\s　]+", " ", str(v or "")).strip()


def refresh(con):
    """Read both exchange lists and store place, industry, website and published email per company.
    Returns {exchange: companies read}."""
    from . import companies
    counts = {}
    for origin, f in FIELDS.items():
        rows = requests.get(f["url"], timeout=90, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"}).json()
        for r in rows:
            name_zh = _clean(r.get(f["zh"]))
            web = _clean(r.get(f["web"])).replace(" ", "")
            if web and "." in web and not web.startswith("http"):
                web = "http://" + web
            search = companies.add(name_en=_clean(r.get(f["en"])), name_zh=name_zh, website=web if "." in web else "",
                                   origin=origin, industry=str(r.get(f["industry"]) or ""), con=con)
            if not search:
                continue
            addr = _clean(r.get(f["addr"]))
            # TWSE gives an English mailing address; for TPEx the main address is already in English
            city, district = place(addr, _clean(r.get(f["addr_en"])) if origin == "TWSE" else addr)
            email = _clean(r.get(f["email"])).lower()
            con.execute("UPDATE companies SET short_name=?, address=?, city=?, district=?, published_email=?, "
                        "industry=COALESCE(NULLIF(?, ''), industry), website=COALESCE(NULLIF(website, ''), NULLIF(?, '')) "
                        "WHERE search=?",
                        (_clean(r.get(f["short"])), addr, city, district,
                         email if EMAIL_RE.fullmatch(email or "") else None, str(r.get(f["industry"]) or ""),
                         web if "." in web else "", search))
        con.commit()
        counts[origin] = len(rows)
    return counts


def select(con, city=None, district=None, industry=None, tier=None, need_email=False, not_checked=False, limit=None,
           country="Taiwan"):
    """Companies matching the filters, best fit first (industry tier, then largest exchange first)."""
    if country.lower() == "india":
        from . import prospects_india
        return prospects_india.select(con, city=city, industry=industry, tier=tier, need_email=need_email,
                                      not_checked=not_checked, limit=limit)
    rows = con.execute("SELECT * FROM companies WHERE enabled=1 AND origin IN ('TWSE','TPEx')").fetchall()
    codes = {industry_code(x) for x in re.split(r"[,，]", industry)} - {None} if industry else None
    if industry and not codes:
        raise ValueError(f"Unknown industry: {industry}. Use a code (30) or a name (資訊服務, IT, fintech).")
    districts = {d.strip().replace("臺", "台").removesuffix("區") for d in re.split(r"[,，]", district)} if district else None
    out = []
    for r in rows:
        code = (r["industry"] or "").zfill(2)
        t = INDUSTRY.get(code, ("", "", 3))[2]
        if city and not any(city_matches(r["city"], c) for c in re.split(r"[,，]", city)):
            continue
        if districts and (r["district"] or "") not in districts:
            continue
        if codes and code not in codes:
            continue
        if tier and t > int(tier):
            continue
        if need_email and not (r["hr_email"] or r["published_email"]):
            continue
        if not_checked and r["email_checked_at"]:
            continue
        out.append((t, 0 if r["origin"] == "TWSE" else 1, r["id"], r))
    out.sort(key=lambda x: x[:3])
    rows = [x[3] for x in out]
    return rows[:limit] if limit else rows


# ---------------------------------------------------------------- hiring address on the company's own site

PATHS = ["careers", "career", "recruit", "jobs", "join-us", "hr", "contact", "contact-us", "stakeholder", "esg/stakeholder",
         "zh-tw/careers", "tw/careers", "zh-tw/contact", "tw/contact"]
HR_LOCAL = re.compile(r"(?i)^(hr|hrd|hrm|recruit|recruiting|recruitment|career|careers|job|jobs|talent|people|resume|cv|"
                      r"hiring|employ|personnel|staffing|join)([._-]?\w*)?$")


LINK_WORDS = re.compile(r"(?i)career|job|join|recruit|talent|hiring|contact|about|stakeholder|esg|csr|sustainab|"
                        r"人才|徵才|招募|求才|人資|聯絡|聯繫|加入|關於|利害關係人|永續|企業社會責任")
HR_NEAR = re.compile(r"(?i)人資|人力資源|人事|招募|徵才|求才|應徵|履歷|human\s*resources?|recruit")
NOT_HR = re.compile(r"(?i)audit|ethic|whistle|safe|complain|report|grievance|esg|csr|sustain|legal|compliance|harass|"
                    r"integrity|sexual|appeal|ombud|invest|ir$|stock|finance|account|sales|service|support|purchas|procure|"
                    r"supplier|vendor|quality|privacy|security|media|press|pr$")


DOC_LINK = re.compile(r"(?i)\.(pdf|docx?|xlsx?|pptx?|zip|rar|jpe?g|png|gif|mp4|mov)(\?|$)")


LONG_TOKEN = re.compile(r"[^\s<>\"'@]{120,}")


class _Page:
    def __init__(self, url, text):
        self.url, self.text = url, text


def _fetch(url, max_bytes=1_500_000, wall=15):
    """An HTML page, or None. Unlike a plain GET it gives up on anything that is not HTML before
    downloading it (ESG reports are often 30 MB PDFs), stops at max_bytes, and never runs past `wall` seconds."""
    if DOC_LINK.search(url):
        return None
    start = time.time()
    try:
        with requests.get(url, headers={"User-Agent": UA}, timeout=(6, 8), stream=True, allow_redirects=True) as r:
            if not r.ok or "html" not in r.headers.get("content-type", "").lower():
                return None
            chunks, size = [], 0
            for chunk in r.iter_content(chunk_size=None):   # whatever has arrived, so the clock is checked often
                chunks.append(chunk)
                size += len(chunk)
                if size > max_bytes or time.time() - start > wall:
                    break
            raw = b"".join(chunks)
            enc = r.encoding if r.encoding and r.encoding.lower() != "iso-8859-1" else (r.apparent_encoding or "utf-8")
            # long unbroken runs (inline base64 images, minified data) make the email pattern take
            # quadratic time while holding the interpreter lock, which freezes every thread: cut them out
            return _Page(r.url, LONG_TOKEN.sub(" ", raw.decode(enc, "replace")))
    except Exception:
        return None


def _links(home):
    """Links on the home page that may lead to careers, contact or stakeholder pages (same site only)."""
    domain = ct._base_domain(urlparse(home.url).netloc)
    out = []
    for href, label in re.findall(r'(?is)<a\s[^>]{0,500}?href=["\']([^"\'#]{1,300})["\'][^>]{0,500}>(.{0,300}?)</a>', home.text):
        text = re.sub(r"<[^>]+>", "", label)[:40]
        if LINK_WORDS.search(href) or LINK_WORDS.search(text):
            full = urljoin(home.url, href)
            if full.startswith("http") and not DOC_LINK.search(full) and ct._base_domain(urlparse(full).netloc) == domain and full not in out:
                out.append(full)
    return out


def _labelled_hr(html):
    """Addresses on a page that sit right after an HR word, e.g. '員工 人資處 王小姐 hr01@x.com.tw'."""
    text = re.sub(r"(?s)<script.*?</script>|<style.*?</style>", " ", html)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    out = []
    for m in EMAIL_RE.finditer(text):
        if HR_NEAR.search(text[max(0, m.start() - 40):m.start()]) and not NOT_HR.search(m.group(0).split("@")[0]):
            out.append(m.group(0).lower())
    return out


def _hr(emails, labelled=()):
    return [e for e in emails if not NOT_HR.search(e.split("@")[0])
            and (HR_LOCAL.match(e.split("@")[0]) or ct.rank(e) == 0 or e in labelled)]


def find_on_site(website, use_search=True):
    """(email, where) for the best address on the company's own domain, or ('', '').
    Order: a hiring address on the site, a hiring address on the domain quoted elsewhere (web search),
    then the site's general inbox (info@, contact@), marked 'general inbox' in `where`."""
    if not website:
        return "", ""
    url = website if website.lower().startswith("http") else "http://" + website
    home = _fetch(url) or _fetch(url.replace("http://", "https://", 1))
    if home is None:
        return "", ""
    domains = {ct._base_domain(urlparse(home.url).netloc), ct._base_domain(urlparse(url).netloc)}
    brands = {d.split(".")[0] for d in domains if len(d.split(".")[0]) >= 3}
    base = f"{urlparse(home.url).scheme}://{urlparse(home.url).netloc}/"
    pages = list(dict.fromkeys(p for p in _links(home)[:12] + [urljoin(base, p) for p in PATHS] if p != home.url))[:20]
    found = [(e, home.url) for e in ct._emails_in(home.text)]
    labelled = set(_labelled_hr(home.text))
    with ThreadPoolExecutor(5) as pool:   # one site's pages a few at a time
        for page, r in zip(pages, pool.map(_fetch, pages)):
            if r is not None:
                found += [(e, r.url) for e in ct._emails_in(r.text)]
                labelled.update(_labelled_hr(r.text))
    where = {}
    for e, page in found:
        where.setdefault(e.strip(".").lower(), page)
    # the company's own domain family: leadtek.com and leadtek.com.tw are one company
    good = [e for e in ct.clean([e for e, _ in found])
            if ct._base_domain(e.split("@")[1]) in domains or ct._base_domain(e.split("@")[1]).split(".")[0] in brands]
    hits = _hr(good, labelled)
    if hits:
        hits.sort(key=lambda e: (not HR_LOCAL.match(e.split("@")[0]), e not in labelled))
        return hits[0], where.get(hits[0], home.url)
    if use_search:
        for link, snippet in ct._search(f'"@{min(domains, key=len)}" 人資 OR 人力資源 OR 履歷 OR 徵才 OR recruit OR hr', n=8):
            hits = [e for e in _hr(ct.clean(ct._emails_in(snippet))) if ct._base_domain(e.split("@")[1]) in domains]
            if hits:
                return hits[0], f"quoted on {urlparse(link).netloc}"
    general = [e for e in good if ct.rank(e) == 2]
    if general:
        return general[0], f"general inbox: {where.get(general[0], home.url)}"
    return "", ""


def find_many(con, rows, workers=12, per_company_seconds=75, on_result=None, use_search=False):
    """Look up hiring addresses for these companies and store them. Returns how many were found."""
    pool = ThreadPoolExecutor(workers)
    futures = [(r, pool.submit(find_on_site, r["website"], use_search)) for r in rows]
    found = 0
    try:
        for r, future in futures:
            try:
                email, where = future.result(timeout=per_company_seconds)
            except (Timeout, Exception):
                email, where = "", ""
            con.execute("UPDATE companies SET hr_email=?, hr_email_where=?, email_checked_at=? WHERE id=?",
                        (email or None, where or None, db.now(), r["id"]))
            con.commit()
            found += bool(email)
            if on_result:
                on_result(r, email, where)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return found


# ---------------------------------------------------------------- the email

# One kind of work per email (user, 2026-10-16: "keep part time emails separate, freelance emails separate and
# other emails separate"). Software and large tech firms are asked about full-time work, smaller non-tech firms
# about part-time work, creative firms about freelance projects. The kind is also the Gmail label (track).
FREELANCE_CODES = {"32"}


def offer_for(company):
    """'full', 'part' or 'freelance' for one company."""
    code = (company["industry"] or "").zfill(2)
    if code in FREELANCE_CODES:
        return "freelance"
    return "full" if INDUSTRY.get(code, ("", "", 3))[2] <= 2 else "part"


OFFER = {
    "full": {"title": "資深軟體工程師（自薦：全職）Senior Software Engineer – full-time",
             "subj_zh": "全職", "subj_en": "full-time",
             "ask_zh": "軟體開發、系統整合或資訊相關的全職職缺",
             "mode_zh": "我可到公司上班，也可遠端工作。",
             "ask_en": "any full-time openings in software development, system integration or IT",
             "mode_en": "I am looking for full-time work, on-site or remote."},
    "part": {"title": "資深軟體工程師（自薦：兼職／約聘）Senior Software Engineer – part-time",
             "subj_zh": "兼職／約聘", "subj_en": "part-time",
             "ask_zh": "軟體開發、資訊系統或電腦相關工作的兼職或約聘職缺",
             "mode_zh": "我可配合兼職或約聘（例如每週固定幾天），到公司上班或遠端皆可。",
             "ask_en": "any part-time or temporary openings in software development, IT systems or computer-based work",
             "mode_en": "I am available part-time or on a temporary contract (for example a fixed number of days a week), on-site or remote."},
    "freelance": {"title": "資深軟體工程師（自薦：專案合作）Senior Software Engineer – freelance projects",
                  "subj_zh": "專案合作（Freelance）", "subj_en": "freelance projects",
                  "ask_zh": "需要外部協助的軟體開發或系統整合專案",
                  "mode_zh": "我以專案方式（freelance）合作，遠端或到公司皆可，也可以先從小型、範圍明確的任務開始。",
                  "ask_en": "projects in software development or system integration where outside help would be useful",
                  "mode_en": "I work on a freelance, project basis, remotely or on-site, and am happy to start with a small, well-defined task."},
}
TITLE = OFFER["full"]["title"]


def phone_parts(raw):
    """'+886 [987291325]' -> ('0987-291-325', '+886 987 291 325'). Anything else is shown as given."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("886") and len(digits) == 12:
        d = digits[3:]
        return f"0{d[:3]}-{d[3:6]}-{d[6:]}", f"+886 {d[:3]} {d[3:6]} {d[6:]}"
    return raw, raw


def write(company, master, sender_email="{sender}", offer=None):
    """(subject, body) for one company row. Chinese first, then English. Taiwan only, so the Gold Card is mentioned.
    The email asks about one kind of work only: `offer` or, by default, offer_for(company)."""
    kind = offer or offer_for(company)
    o = OFFER[kind]
    svc_zh = f"\n\n我可提供的服務：\n{bullets_zh()}" if kind == "freelance" else ""
    svc_en = f"\n\nServices I offer:\n{bullets_en()}" if kind == "freelance" else ""
    code = (company["industry"] or "").zfill(2)
    zh_ind, en_ind, _ = INDUSTRY.get(code, ("", "", 3))
    angle_zh, angle_en = ANGLE[ANGLE_OF.get(code, "general")]
    name_zh = company["name_zh"] or company["search"]
    short = company["short_name"] or company["search"]
    tw_phone, intl_phone = phone_parts(master["contact"].get("phone", ""))
    name = master["name"].replace(" R.", "")
    links = master["contact"].get("links") or []
    linkedin = next((l for l in links if "linkedin" in l), "")
    portfolio = next((l for l in links if "portfolio" in l or "vercel" in l), "")
    folio_zh = f"作品集與專案範例請參考：{portfolio}\n\n" if portfolio else ""
    folio_en = f"You can see my portfolio and project examples at {portfolio}\n\n" if portfolio else ""
    folio_sig = f"作品集：{portfolio}\n" if portfolio else ""
    sector_zh = f"在{zh_ind}領域的發展" if zh_ind and code not in ("20", "19", "80", "91") else "的發展"
    ask_en = (f"Given your company's work in {en_ind}, I am writing to ask whether you have"
              if en_ind and code not in ("20", "19", "80", "91") else "I am writing to ask whether your company has")
    # the company in the subject: each email is its own conversation, also in your Sent folder and CC copy
    subject = (f"致{short}：自薦資深軟體工程師（C#／.NET、SQL Server）{o['subj_zh']}｜"
               f"Senior Software Engineer (.NET) – {o['subj_en']} – {name}")
    body = f"""{name_zh} 人力資源部 您好：

我是 {name}，現居新北市，持有臺灣就業金卡（Taiwan Employment Gold Card），可直接在臺灣合法工作，貴公司無須為我申請工作許可。

身為資深軟體工程師，我擁有 10 年以上以 Microsoft .NET（C#、ASP.NET Core、.NET 8）及 Microsoft SQL Server 開發企業系統的經驗，涵蓋後端架構、REST／SOAP API 設計、第三方系統整合，以及 Angular、Next.js 前端開發，並曾服務金融科技、醫療健康、物流等產業。

{angle_zh}

我關注到{short}{sector_zh}，想請問貴公司目前是否有{o['ask_zh']}。{o['mode_zh']}若此信件不屬於您的業務範圍，懇請協助轉交人資或資訊部門，非常感謝。{svc_zh}

{folio_zh}隨信附上英文履歷供您參考。我的中文能力有限，工作上以英文溝通為主。期待有機會與您進一步洽談。

敬祝 商祺

{name}
電話：{tw_phone}
Email：{sender_email}
{folio_sig}
----------------------------------------
English version
----------------------------------------

Dear Hiring Team,

My name is {name}. I am based in New Taipei City and hold a Taiwan Employment Gold Card, so I can work in Taiwan without employer sponsorship or a work permit application.

I am a Senior Software Engineer with 10+ years of experience building enterprise applications on Microsoft .NET (C#, ASP.NET Core, .NET 8) and Microsoft SQL Server, covering backend architecture, REST and SOAP API design, third-party integrations, and front ends in Angular and Next.js, across FinTech, HealthTech and logistics.

{angle_en}

{ask_en} {o['ask_en']}. {o['mode_en']} If this message has reached the wrong department, I would be grateful if you could forward it to HR or IT.{svc_en}

{folio_en}My resume is attached. My Mandarin is limited, so I work in English. I would welcome the chance to talk.

Best regards,
{name}
{intl_phone}
{sender_email}""" + "".join(f"\n{l}" for l in (linkedin, portfolio) if l)
    return subject, body


# ---------------------------------------------------------------- into the send queue

GENERAL_LOCAL = re.compile(r"(?i)^(info|information|contact|contactus|service|services|cs|public|hello|web|office|main|"
                           r"general|inquiry|enquiry|mail|admin|hq)[._-]?\w{0,6}$")


def address(c):
    """(address, how it was found) for one company. 'CHECK ...' means it waits for you: the address in the
    exchange record is usually investor relations, finance or the spokesperson's own mailbox, and only a
    general inbox there (info@, service@ ...) is used without asking."""
    if c["hr_email"]:
        return c["hr_email"], f"found on the company's own domain: {c['hr_email_where']}"
    pub = c["published_email"] or ""
    if not pub:
        return "", ""
    if GENERAL_LOCAL.match(pub.split("@")[0]) and ct.has_mx(pub.split("@")[1]):
        return pub, "general inbox published in the stock-exchange company record"
    return pub, "CHECK address published in the stock-exchange record (investor relations / finance / a person)"


def queue(con, rows, master, resume_path, published_ok=False):
    """One prospect row in `jobs` per company with an address. Returns (added, waiting_for_you, already_there).
    Addresses on the company's domain and general inboxes are approved; the rest wait unless published_ok."""
    added = waiting = known = 0
    for c in rows:
        to, how = address(c)
        if not to:
            continue
        name_zh = c["name_zh"] or c["search"]
        norm = re.sub(r"[\W_]+", "", name_zh.lower())
        old = con.execute("SELECT id, emails, send_status FROM jobs WHERE source='prospect' AND ext_id=?",
                          (str(c["id"]),)).fetchone()
        if old:
            known += 1
            # not sent yet and a better address turned up since (hiring address on the site): use it
            if old["send_status"] in (None, "approved") and c["hr_email"] and old["emails"] != f'["{to}"]':
                con.execute("UPDATE jobs SET emails=?, email_source=?, send_status='approved' WHERE id=?",
                            (f'["{to}"]', how, old["id"]))
            continue
        if c["origin"] == "IN-Wikidata":
            from . import prospects_india
            added += prospects_india.queue_one(con, c, master, resume_path, to, how)
            continue
        kind = offer_for(c)
        subject, body = write(c, master, offer=kind)
        code = (c["industry"] or "").zfill(2)
        location = " ".join(x for x in (c["city"], (c["district"] or "") and c["district"] + "區") if x) or "Taiwan"
        approved = "approved" if (published_ok or not how.startswith("CHECK")) else None
        con.execute("""INSERT OR IGNORE INTO jobs(source, ext_id, title, company, location, region, remote, job_type, track,
                       category, url, description, tags, emails, score, score_notes, status, dedupe_key, found_at,
                       matched_on, resume_path, email_subject, email_body, tailored_by, email_source, contact_checked,
                       send_status, company_url)
                       VALUES('prospect',?,?,?,?,'taiwan',0,'prospect',?,'Prospect',?,?, '[]',?,?,?,'matched',?,?,?,?,?,?,
                       'template',?,1,?,?)""",
                    (str(c["id"]), OFFER[kind]["title"], name_zh, location, kind, c["website"] or "",
                     f"{INDUSTRY.get(code, ('', '', 3))[0]} {c['address'] or ''}".strip(),
                     f'["{to}"]', 60 - 10 * INDUSTRY.get(code, ("", "", 3))[2], f"industry {code}",
                     f"{norm}|prospect", db.now(), db.today(), str(resume_path), subject, body, how, approved,
                     c["website"] or None))
        added += 1
        waiting += approved is None
    con.commit()
    return added, waiting, known
