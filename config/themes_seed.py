"""
Themes (Waves) — seed taxonomy.

Complements the broad sector reclassification (`stock_universe.valvo_sector`)
with a cross-cutting *theme* layer. A stock has exactly one sector but may
carry multiple themes — e.g. CGPOWER is an "Electrical" sector stock riding
both the AI Data-Center Power theme and the Semiconductors theme.

Structure mirrors `custom_sectors_seed.py`:
    WAVES_SEED   — top-level tailwinds (6)
    THEMES_SEED  — specific themes under each wave (~22)

Each theme entry:
    slug            stable url-safe id (never renamed once live)
    wave            parent wave slug
    name            display name
    description     one-line hook for UI/admin
    keywords        lowercase substrings matched against segment names
                    (and, for fallbacks, company_name + industry + sector)
    name_overrides  hand-curated stock symbols that are pure-play on this
                    theme regardless of what the segment data says —
                    rescues household names with stale/geo-only segments

Web verification (Backend/scripts/verify_themes_web.py) extends name_overrides
with up to 150 web-verified entries before the classifier runs.

Keep this file additive — slug changes require data migration because
stock_themes rows reference themes by slug.
"""

# Theme → canonical parent_sector (one of the 20 in custom_sectors_seed.py).
# Lets the unified classifier derive `valvo_sector` from a stock's primary theme
# so sector + theme can never disagree (e.g. GRSE's defence theme implies
# Defence & Aerospace sector, not Infrastructure & Construction).
# Additive: read by build_classification_v2 — does not modify THEMES_SEED below.
THEME_PARENT_SECTOR = {
    "ai_compute_hardware":     "IT & Technology",
    "dc_power_infra":          "Engineering & Capital Goods",
    "dc_cooling_hvac":         "Engineering & Capital Goods",
    "semiconductors_osat":     "IT & Technology",
    "ems_electronics":         "IT & Technology",
    "embedded_ai_software":    "IT & Technology",
    "renewable_generation":    "Power & Utilities",
    "transmission_buildout":   "Power & Utilities",
    "ev_ecosystem":            "Auto & Ancillary",
    "nuclear_smr":             "Power & Utilities",
    "defence_indigenization":  "Defence & Aerospace",
    "specialty_chemicals":     "Chemicals & Fertilizers",
    "pharma_api_cdmo":         "Pharma & Healthcare",
    "electronics_pli":         "IT & Technology",
    "railways_modernization":  "Railways & Logistics",
    "roads_highways":          "Infrastructure & Construction",
    "ports_logistics":         "Railways & Logistics",
    "water_infra":             "Infrastructure & Construction",
    "discretionary_upgrade":   "FMCG & Consumer",
    "travel_experience":       "FMCG & Consumer",
    "capital_markets":         "Capital Markets",
    "wealth_amc_insurance":    "Insurance",
}


WAVES_SEED = [
    {
        "slug": "ai_digital_infra",
        "name": "AI & Digital Infra",
        "accent_color": "#7c3aed",
        "sort_order": 1,
        "description": "Indian capex riding the global AI buildout — servers, DC power, cooling, EMS, semicon, embedded AI.",
    },
    {
        "slug": "energy_transition",
        "name": "Energy Transition",
        "accent_color": "#16a34a",
        "sort_order": 2,
        "description": "Renewables, grid expansion, EV supply chain, batteries, nuclear/SMR.",
    },
    {
        "slug": "make_in_india",
        "name": "Make in India (PLI)",
        "accent_color": "#f97316",
        "sort_order": 3,
        "description": "Domestic manufacturing push — defence, PLI electronics, specialty chem, pharma API/CDMO.",
    },
    {
        "slug": "capex_cycle",
        "name": "Capex Cycle",
        "accent_color": "#0ea5e9",
        "sort_order": 4,
        "description": "Roads, railways, ports, water — long-cycle infrastructure buildout.",
    },
    {
        "slug": "consumer_premium",
        "name": "Consumer Premiumization",
        "accent_color": "#db2777",
        "sort_order": 5,
        "description": "Discretionary upgrade, travel/hospitality, premium retail.",
    },
    {
        "slug": "financialization",
        "name": "Financialization",
        "accent_color": "#ca8a04",
        "sort_order": 6,
        "description": "Household savings flowing to equity — exchanges, depositories, AMCs, insurance, wealth.",
    },
]


THEMES_SEED = [
    # ── AI & DIGITAL INFRA (6 themes) ──────────────────────────────
    {
        "slug": "ai_compute_hardware",
        "wave": "ai_digital_infra",
        "name": "AI Compute Hardware",
        "description": "Servers, HPC/GPU systems, AI-ready compute boxes.",
        "keywords": [
            "computer servers", "computer and computer servers", "hpc", "ai server",
            "gpu server", "server manufacturing", "high performance computing",
            "supercomputing", "ai cluster", "private cloud infrastructure",
        ],
        "name_overrides": ["NETWEB"],
    },
    {
        "slug": "dc_power_infra",
        "wave": "ai_digital_infra",
        "name": "Data Center Power Infra",
        "description": "Transformers, switchgear, LV motors, UPS, grid automation feeding hyperscaler DCs.",
        "keywords": [
            "transformers", "power grids", "power systems", "low voltage motors",
            "grid automation", "power electronics", "switchgear",
            "electrical transformers", "dry type transformers",
            "distribution transformers", "power transformers",
            "medium voltage motors", "uninterruptible power supply",
        ],
        "name_overrides": [
            "VOLTAMP", "POWERINDIA", "APARINDS", "CGPOWER", "ABB",
            "SIEMENS", "BHEL", "THERMAX", "HITACHIEN", "TRIL",
        ],
    },
    {
        "slug": "dc_cooling_hvac",
        "wave": "ai_digital_infra",
        "name": "Data Center Cooling / HVAC",
        "description": "Heat exchangers, precision cooling, industrial HVAC for server halls.",
        "keywords": [
            "heat exchanger", "hvac", "cooling systems", "precision cooling",
            "refrigeration", "air conditioning", "chillers", "cooling towers",
            "thermal management",
        ],
        "name_overrides": ["KRN", "BLUESTARCO", "AMBER", "VOLTAS", "JOHNSONCON"],
    },
    {
        "slug": "semiconductors_osat",
        "wave": "ai_digital_infra",
        "name": "Semiconductors / OSAT",
        "description": "Chip design, ATMP/OSAT, discrete power semiconductors.",
        "keywords": [
            "semiconductor", "osat", "chip design", "asic", "soc", "wafer",
            "foundry", "ic design", "silicon carbide", "gallium nitride",
            "integrated circuits", "discrete semiconductor",
        ],
        "name_overrides": ["MOSCHIP", "CGPOWER", "KAYNES", "SPEL", "ASMTECH"],
    },
    {
        "slug": "ems_electronics",
        "wave": "ai_digital_infra",
        "name": "EMS / Electronics Mfg",
        "description": "Electronics manufacturing services, PCB, box-build, contract mfg.",
        "keywords": [
            "electronics manufacturing services", "ems", "pcb assembly",
            "electronic components", "contract manufacturing",
            "box build", "electronic manufacturing", "pcb",
            "printed circuit board",
        ],
        "name_overrides": [
            "DIXON", "SYRMA", "AVALON", "KAYNES", "CYIENTDLM",
            "PGEL", "AMBER", "ELIN",
        ],
    },
    {
        "slug": "embedded_ai_software",
        "wave": "ai_digital_infra",
        "name": "Embedded AI & Design Services",
        "description": "ER&D, ADAS, chip-to-cloud software, embedded AI services.",
        "keywords": [
            "embedded software", "adas", "autonomous driving",
            "software development and services", "design services",
            "engineering services", "er&d", "embedded systems",
            "digital engineering", "product engineering",
        ],
        "name_overrides": ["TATAELXSI", "KPITTECH", "LTTS", "TATATECH", "CYIENT"],
    },

    # ── ENERGY TRANSITION (4 themes) ───────────────────────────────
    {
        "slug": "renewable_generation",
        "wave": "energy_transition",
        "name": "Renewable Generation",
        "description": "Solar, wind, hybrid IPPs plus EPC/equipment makers.",
        "keywords": [
            "renewable power", "renewable energy", "solar", "wind power",
            "hybrid power", "green energy", "solar module", "wind turbine",
            "solar cell", "photovoltaic",
        ],
        "name_overrides": [
            "ADANIGREEN", "JSWENERGY", "TATAPOWER", "NTPCGREEN", "NHPC",
            "INOXWIND", "WAAREE", "SUZLON", "ORIENTGREEN", "KPIGREEN",
        ],
    },
    {
        "slug": "transmission_buildout",
        "wave": "energy_transition",
        "name": "Transmission & Grid Buildout",
        "description": "Conductors, cables, T&D EPC, substations.",
        "keywords": [
            "transmission", "conductors", "power cables", "transmission lines",
            "substations", "t&d", "power transmission", "transmission towers",
        ],
        "name_overrides": ["POWERGRID", "KEC", "KALPATPOWR", "APARINDS", "SKIPPER", "RPOWER"],
    },
    {
        "slug": "ev_ecosystem",
        "wave": "energy_transition",
        "name": "EV Ecosystem",
        "description": "Batteries, cell mfg, EV OEMs, motors, charging infra.",
        "keywords": [
            "electric vehicle", "ev battery", "lithium", "cell manufacturing",
            "battery pack", "charging infrastructure", "ev charger",
            "lithium ion", "battery cell", "e-vehicle",
        ],
        "name_overrides": [
            "AMARAJABAT", "EXIDEIND", "OLAELEC", "SONACOMS", "TIINDIA",
            "GREAVESCOT", "HBLPOWER",
        ],
    },
    {
        "slug": "nuclear_smr",
        "wave": "energy_transition",
        "name": "Nuclear / SMR",
        "description": "Nuclear equipment, heavy water, SMR ecosystem plays.",
        "keywords": [
            "nuclear power", "nuclear reactor", "heavy water",
            "small modular reactor", "nuclear equipment",
        ],
        "name_overrides": ["WALCHAN", "NTPC", "BHEL"],
    },

    # ── MAKE IN INDIA (4 themes) ───────────────────────────────────
    {
        "slug": "defence_indigenization",
        "wave": "make_in_india",
        "name": "Defence Indigenization",
        "description": "Aerospace, naval, avionics, missile, radar — PSU + private.",
        "keywords": [
            "defence", "defense", "aerospace", "avionics", "missile",
            "radar", "naval", "submarine", "armament", "warship",
            "military", "artillery", "ammunition", "unmanned aerial",
        ],
        "name_overrides": [
            "HAL", "BEL", "DATAPATTNS", "MAZDOCK", "BDL", "BEML",
            "GRSE", "COCHINSHIP", "IDEAFORGE", "ZENTEC", "ASTRAMICRO",
            "MISHTANN", "PARAS", "SIKA",
        ],
    },
    {
        "slug": "specialty_chemicals",
        "wave": "make_in_india",
        "name": "Specialty Chemicals",
        "description": "Fluorochem, agrochem, performance/fine chemicals.",
        "keywords": [
            "speciality chemicals", "specialty chemicals", "fluorochemicals",
            "agrochemicals", "fine chemicals", "performance chemicals",
            "performance materials", "industrial chemicals", "dye",
        ],
        "name_overrides": [
            "SRF", "PIDILITIND", "DEEPAKNTR", "AARTIIND", "NAVINFLUOR",
            "PCBL", "ATUL", "VINATIORGA", "GALAXYSURF", "CLEAN", "FINEORG",
        ],
    },
    {
        "slug": "pharma_api_cdmo",
        "wave": "make_in_india",
        "name": "Pharma API / CDMO",
        "description": "API makers, CDMOs, contract research.",
        "keywords": [
            "api", "active pharmaceutical", "cdmo", "contract research",
            "contract manufacturing pharma", "bulk drug", "active ingredient",
            "cro", "contract development",
        ],
        "name_overrides": ["DIVISLAB", "LAURUSLABS", "PIIND", "SYNGENE", "NEULANDLAB", "SUVEN"],
    },
    {
        "slug": "electronics_pli",
        "wave": "make_in_india",
        "name": "Electronics PLI Beneficiary",
        "description": "Consumer electronics, mobile phone mfg under PLI.",
        "keywords": [
            "mobile phone", "smartphone manufacturing", "consumer electronics",
            "electronic goods", "electronics products", "consumer durables",
            "lighting products",
        ],
        "name_overrides": ["DIXON", "AMBER", "PGEL", "KAYNES", "PRINCEPIPE", "ORIENTELEC"],
    },

    # ── CAPEX CYCLE (4 themes) ─────────────────────────────────────
    {
        "slug": "railways_modernization",
        "wave": "capex_cycle",
        "name": "Railways Modernization",
        "description": "Wagons, rolling stock, metro, signalling.",
        "keywords": [
            "railway", "railways", "wagons", "rolling stock",
            "metro", "coaches", "locomotive", "signalling",
            "railway infrastructure",
        ],
        "name_overrides": [
            "TITAGARH", "TEXRAIL", "JUPITERWAG", "IRCTC", "RVNL",
            "IRFC", "CONCOR", "IRCON",
        ],
    },
    {
        "slug": "roads_highways",
        "wave": "capex_cycle",
        "name": "Roads & Highways",
        "description": "Road EPC, highway BOT/HAM concessions.",
        "keywords": [
            "road construction", "highways", "epc", "infrastructure projects",
            "road projects", "bot", "ham", "expressway", "road epc",
        ],
        "name_overrides": ["LT", "GPIL", "KNRCON", "PNCINFRA", "HGINFRA", "IRBINFRA", "ASHOKLEY"],
    },
    {
        "slug": "ports_logistics",
        "wave": "capex_cycle",
        "name": "Ports & Logistics",
        "description": "Port operators, container terminals, 3PL/warehousing.",
        "keywords": [
            "port", "ports", "terminal", "container logistics",
            "warehousing", "shipping", "cargo", "3pl",
        ],
        "name_overrides": ["ADANIPORTS", "JSWINFRA", "GPPL", "ALLCARGO", "CONCOR", "SCI"],
    },
    {
        "slug": "water_infra",
        "wave": "capex_cycle",
        "name": "Water Infrastructure",
        "description": "Water/wastewater EPC and equipment.",
        "keywords": [
            "water", "sewage", "desalination", "wastewater", "water treatment",
            "effluent treatment",
        ],
        "name_overrides": ["VATECHWABAG", "ION", "JASH"],
    },

    # ── CONSUMER PREMIUMIZATION (2 themes) ─────────────────────────
    {
        "slug": "discretionary_upgrade",
        "wave": "consumer_premium",
        "name": "Discretionary Upgrade",
        "description": "Jewellery, apparel, premium retail, footwear.",
        "keywords": [
            "retail apparel", "jewellery", "footwear", "luxury",
            "premium retail", "branded apparel", "fashion retail",
        ],
        "name_overrides": [
            "TRENT", "TITAN", "LANDMARK", "METROBRAND", "VEDL",
            "RELAXO", "CAMPUS", "ABFRL", "SENCO",
        ],
    },
    {
        "slug": "travel_experience",
        "wave": "consumer_premium",
        "name": "Travel & Experience",
        "description": "Hotels, airlines, QSR, experiences.",
        "keywords": [
            "hospitality", "hotels", "airlines", "tourism", "travel",
            "quick service restaurant", "qsr",
        ],
        "name_overrides": [
            "INDIGO", "LEMONTREE", "INDHOTEL", "CHALET", "EIHOTEL",
            "MHRIL", "DEVYANI", "JUBLFOOD", "WESTLIFE",
        ],
    },

    # ── FINANCIALIZATION (2 themes) ────────────────────────────────
    {
        "slug": "capital_markets",
        "wave": "financialization",
        "name": "Capital Markets Infra",
        "description": "Exchanges, depositories, clearing corps, brokers, registrars.",
        "keywords": [
            "capital markets", "stock exchange", "depository", "brokerage",
            "clearing corporation", "registrar", "transfer agent",
            "trading platform", "commodity exchange",
        ],
        "name_overrides": [
            "BSE", "MCX", "CDSL", "KFINTECH", "CAMS", "ANGEL",
            "MOTILALOFS", "IEX",
        ],
    },
    {
        "slug": "wealth_amc_insurance",
        "wave": "financialization",
        "name": "Wealth / AMC / Insurance",
        "description": "Asset managers, insurers, wealth distributors.",
        "keywords": [
            "asset management", "wealth management", "mutual fund",
            "life insurance", "general insurance", "health insurance",
            "amc", "insurance",
        ],
        "name_overrides": [
            "HDFCAMC", "HDFCLIFE", "ICICIPRULI", "360ONE", "NUVAMA",
            "SBILIFE", "LICI", "ICICIGI", "MAXLIFE", "NIPPONIND",
        ],
    },
]
