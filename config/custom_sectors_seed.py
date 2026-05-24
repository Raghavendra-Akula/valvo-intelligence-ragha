"""
Custom Sectors — seed taxonomy.

These are deeper sub-sectors that drill beneath the 20 broad sector
buckets used elsewhere in Valvo. They're the foundation for the future
"custom sector analysis" feature (grouping stocks by the business model
they actually run, not by the catch-all industry name).

Each entry:
    slug            url-safe identifier (stable; never changes once live)
    name            display name
    parent_sector   one of the 20 broad buckets (must match sectors.js)
    description     short blurb for UI/admin
    keywords        lowercase substrings used for auto-classification
                    against stock name / industry / business description

The classification service matches keywords against
`stock_universe.name` + `stock_universe.sector` + (if present)
`stock_universe.industry`. Longer keywords win ties.

Keep this file additive — removing or renaming a slug requires a
data migration because stock_custom_sector rows reference it by id.
"""

CUSTOM_SECTORS_SEED = [
    # ── Banks & Finance ─────────────────────────────────────────
    {
        "slug": "private-banks",
        "name": "Private Banks",
        "parent_sector": "Banks & Finance",
        "description": "Scheduled private-sector commercial banks.",
        "keywords": ["hdfc bank", "icici bank", "axis bank", "kotak mahindra bank",
                     "indusind bank", "yes bank", "idfc first bank", "federal bank",
                     "rbl bank", "bandhan bank", "south indian bank", "karur vysya",
                     "city union bank", "dhanlaxmi bank", "csb bank", "tamilnad mercantile",
                     "karnataka bank"],
    },
    {
        "slug": "psu-banks",
        "name": "PSU Banks",
        "parent_sector": "Banks & Finance",
        "description": "Public-sector banks.",
        "keywords": ["state bank of india", " sbi ", "bank of baroda", "punjab national bank",
                     "canara bank", "union bank of india", "bank of india", "indian bank",
                     "central bank of india", "uco bank", "bank of maharashtra",
                     "punjab & sind bank", "indian overseas bank"],
    },
    {
        "slug": "nbfc-diversified",
        "name": "NBFC — Diversified",
        "parent_sector": "Banks & Finance",
        "description": "Non-banking finance companies with a diversified loan book.",
        "keywords": ["bajaj finance", "cholamandalam", "shriram finance", "l&t finance",
                     "mahindra finance", "poonawalla fincorp", "piramal enterprises",
                     "aditya birla capital", "muthoot capital", "iifl finance",
                     "nbfc", "non-banking", "retail banking", "wholesale banking",
                     "corporate banking", "corporate/wholesale banking",
                     "other banking operations", "treasury", "treasury operations",
                     "financing activities", "lending", "loans"],
    },
    {
        "slug": "housing-finance",
        "name": "Housing Finance",
        "parent_sector": "Banks & Finance",
        "description": "Dedicated housing-finance companies (HFCs).",
        "keywords": ["housing finance", "home finance", "hfc", "pnb housing",
                     "aavas financiers", "aptus value", "can fin homes", "lic housing",
                     "repco home"],
    },
    {
        "slug": "gold-finance",
        "name": "Gold Finance",
        "parent_sector": "Banks & Finance",
        "description": "Gold-loan NBFCs.",
        "keywords": ["muthoot finance", "manappuram"],
    },
    {
        "slug": "microfinance",
        "name": "Microfinance",
        "parent_sector": "Banks & Finance",
        "description": "MFI and small-ticket unsecured lenders.",
        "keywords": ["microfinance", "ujjivan small finance", "equitas small finance",
                     "credit access", "spandana", "fusion micro", "esaf", "suryoday"],
    },

    # ── Capital Markets ─────────────────────────────────────────
    {
        "slug": "stock-exchanges",
        "name": "Exchanges & Depositories",
        "parent_sector": "Capital Markets",
        "description": "Exchanges, clearing corps and depositories.",
        "keywords": ["bse limited", "multi commodity exchange", "mcx", "cdsl", "nsdl"],
    },
    {
        "slug": "brokers",
        "name": "Brokers & Distributors",
        "parent_sector": "Capital Markets",
        "description": "Retail and wealth brokers, product distributors.",
        "keywords": ["angel one", "motilal oswal", "icici securities", "iifl securities",
                     "5paisa", "geojit", "share india", "prudent corporate"],
    },
    {
        "slug": "amc",
        "name": "Asset Managers",
        "parent_sector": "Capital Markets",
        "description": "Mutual fund asset management companies.",
        "keywords": ["asset management", " amc", "nippon life india", "hdfc amc",
                     "uti amc", "aditya birla sun life amc", "capital markets",
                     "mutual fund", "wealth management", "investment management"],
    },

    # ── Insurance ───────────────────────────────────────────────
    {
        "slug": "life-insurance",
        "name": "Life Insurance",
        "parent_sector": "Insurance",
        "description": "Life insurers.",
        "keywords": ["life insurance", "hdfc life", "sbi life", "icici prudential life",
                     "max financial", "lic of india"],
    },
    {
        "slug": "general-insurance",
        "name": "General & Health Insurance",
        "parent_sector": "Insurance",
        "description": "Non-life insurers.",
        "keywords": ["general insurance", "health insurance", "icici lombard",
                     "star health", "new india assurance", "go digit",
                     "insurance", "reinsurance", "non-life insurance"],
    },

    # ── IT & Technology ─────────────────────────────────────────
    {
        "slug": "it-services-large",
        "name": "IT Services — Large Cap",
        "parent_sector": "IT & Technology",
        "description": "Tier-1 IT services exporters.",
        "keywords": ["tata consultancy", "infosys", "wipro", "hcl technologies",
                     "tech mahindra", "lti mindtree", "mphasis"],
    },
    {
        "slug": "it-services-midcap",
        "name": "IT Services — Mid/Small",
        "parent_sector": "IT & Technology",
        "description": "Mid and small-cap IT services firms.",
        "keywords": ["coforge", "persistent systems", "birlasoft", "sonata software",
                     "cyient", "kpit", "tata technologies", "zensar", "intellect design",
                     "it services", "software services", "technology services",
                     "digital services", "engineering services", "product engineering",
                     "it consulting", "business services", "infotech"],
    },
    {
        "slug": "product-saas",
        "name": "Product / SaaS",
        "parent_sector": "IT & Technology",
        "description": "Software product & SaaS companies (not pure services).",
        "keywords": ["saas", "oracle financial", "nucleus software", "ramco systems",
                     "happiest minds", "tanla", "route mobile", "affle india", "nazara"],
    },
    {
        "slug": "digital-platforms",
        "name": "Digital / Internet Platforms",
        "parent_sector": "IT & Technology",
        "description": "Consumer internet and new-age platform businesses.",
        "keywords": ["zomato", "nykaa", "policybazaar", "paytm", "one 97", "info edge",
                     "ixigo", "carttrade", "mobikwik", "swiggy"],
    },

    # ── Pharma & Healthcare ─────────────────────────────────────
    {
        "slug": "generic-pharma",
        "name": "Generic Pharma",
        "parent_sector": "Pharma & Healthcare",
        "description": "Generic formulations players (domestic + export).",
        "keywords": ["sun pharma", "dr reddy", "cipla", "lupin", "aurobindo pharma",
                     "torrent pharma", "alkem", "zydus lifesciences", "glenmark",
                     "pharmaceuticals", "pharmaceutical", "formulations",
                     "active pharma", "bulk drugs", "generics", "branded formulations",
                     "drug products", "pharma"],
    },
    {
        "slug": "cdmo-cro",
        "name": "CDMO / CRO",
        "parent_sector": "Pharma & Healthcare",
        "description": "Contract development, manufacturing & research orgs.",
        "keywords": ["divi's lab", "syngene", "laurus labs", "piramal pharma",
                     "suven pharma", "neuland labs", "gland pharma"],
    },
    {
        "slug": "hospitals",
        "name": "Hospitals",
        "parent_sector": "Pharma & Healthcare",
        "description": "Hospital chains.",
        "keywords": ["apollo hospitals", "fortis healthcare", "max healthcare",
                     "narayana hrudayalaya", "global health", "krsnaa",
                     "hospital", "hospitals", "healthcare services", "medical services"],
    },
    {
        "slug": "diagnostics",
        "name": "Diagnostics",
        "parent_sector": "Pharma & Healthcare",
        "description": "Diagnostic chains and pathology labs.",
        "keywords": ["dr lal pathlabs", "metropolis healthcare", "vijaya diagnostic",
                     "thyrocare"],
    },

    # ── Auto & Ancillary ────────────────────────────────────────
    {
        "slug": "auto-2w",
        "name": "Two-Wheelers",
        "parent_sector": "Auto & Ancillary",
        "description": "Two- and three-wheeler OEMs.",
        "keywords": ["bajaj auto", "hero motocorp", "tvs motor", "eicher motors",
                     "ola electric"],
    },
    {
        "slug": "auto-4w",
        "name": "Passenger & Commercial Vehicles",
        "parent_sector": "Auto & Ancillary",
        "description": "Passenger car, SUV and truck OEMs.",
        "keywords": ["maruti suzuki", "tata motors", "mahindra & mahindra",
                     "ashok leyland", "force motors",
                     "automobile", "passenger vehicles", "commercial vehicles",
                     "passenger cars", "utility vehicles", "trucks", "tractors",
                     "automotive"],
    },
    {
        "slug": "auto-components",
        "name": "Auto Components",
        "parent_sector": "Auto & Ancillary",
        "description": "OE suppliers and aftermarket.",
        "keywords": ["bosch ", "motherson", "bharat forge", "endurance tech",
                     "sundram fasteners", "sona blw", "minda industries", "uno minda",
                     "exide industries", "amara raja",
                     "auto components", "automotive components", "auto parts",
                     "automotive products", "batteries", "forgings"],
    },
    {
        "slug": "tyres",
        "name": "Tyres",
        "parent_sector": "Auto & Ancillary",
        "description": "Tyre manufacturers.",
        "keywords": ["mrf ", "apollo tyres", "ceat ", "balkrishna industries", "jk tyre",
                     "tyres", "tires", "tyre", "tire"],
    },

    # ── Chemicals & Fertilizers ─────────────────────────────────
    {
        "slug": "specialty-chemicals",
        "name": "Specialty Chemicals",
        "parent_sector": "Chemicals & Fertilizers",
        "description": "Specialty chemicals, performance chemicals, custom synthesis.",
        "keywords": ["pi industries", "srf ", "aarti industries", "navin fluorine",
                     "vinati organics", "atul ", "deepak nitrite", "gujarat fluorochemicals",
                     "clean science", "fine organic", "galaxy surfactants",
                     "speciality chemicals", "specialty chemicals", "speciality chemical",
                     "specialty chemical", "chemicals", "chemical", "performance chemicals",
                     "custom synthesis", "dyes", "pigments", "petrochemicals"],
    },
    {
        "slug": "agrochemicals",
        "name": "Agrochemicals & Fertilizers",
        "parent_sector": "Chemicals & Fertilizers",
        "description": "Crop protection and fertilizers.",
        "keywords": ["upl ", "coromandel international", "bayer cropscience",
                     "rallis india", "sumitomo chemical", "dhanuka", "gnfc", "chambal fert",
                     "deepak fertilisers",
                     "agrochemicals", "agro chemicals", "agri chemicals",
                     "fertilizers", "fertilisers", "fertiliser", "fertilizer",
                     "crop protection", "pesticides"],
    },

    # ── Infrastructure & Construction ───────────────────────────
    {
        "slug": "realty-residential",
        "name": "Real Estate — Residential",
        "parent_sector": "Infrastructure & Construction",
        "description": "Residential developers.",
        "keywords": ["dlf ", "godrej properties", "oberoi realty", "prestige estate",
                     "macrotech", "lodha", "sobha", "brigade enterprises", "sunteck",
                     "real estate", "real estate development", "realty", "property development",
                     "residential"],
    },
    {
        "slug": "realty-commercial",
        "name": "Real Estate — Commercial / REIT",
        "parent_sector": "Infrastructure & Construction",
        "description": "Commercial real estate, REITs.",
        "keywords": ["embassy office parks", "mindspace business parks",
                     "brookfield india reit", "nexus select"],
    },
    {
        "slug": "epc-road",
        "name": "Roads & EPC",
        "parent_sector": "Infrastructure & Construction",
        "description": "Road/highway EPC and asset owners.",
        "keywords": ["irb infra", "pnc infratech", "hg infra", "kns infrastructure",
                     "dilip buildcon", "ashoka buildcon", "kec international",
                     "epc", "construction", "infrastructure", "civil construction",
                     "roads", "highways", "construction services", "project engineering"],
    },

    # ── Metals & Mining ─────────────────────────────────────────
    {
        "slug": "ferrous-metals",
        "name": "Ferrous Metals",
        "parent_sector": "Metals & Mining",
        "description": "Steel producers.",
        "keywords": ["tata steel", "jsw steel", "jindal steel", "sail ", "nmdc ",
                     "jindal stainless", "apl apollo",
                     "steel", "iron", "iron ore", "ferrous", "ferrous metals",
                     "mining", "minerals"],
    },
    {
        "slug": "non-ferrous-metals",
        "name": "Non-Ferrous Metals",
        "parent_sector": "Metals & Mining",
        "description": "Copper, aluminium, zinc, precious metals.",
        "keywords": ["hindalco", "vedanta", "nalco", "hindustan copper", "hindustan zinc",
                     "copper", "aluminium", "aluminum", "zinc", "lead", "non-ferrous",
                     "non ferrous", "precious metals", "metals"],
    },

    # ── Oil, Gas & Energy ───────────────────────────────────────
    {
        "slug": "upstream-oil-gas",
        "name": "Upstream Oil & Gas",
        "parent_sector": "Oil, Gas & Energy",
        "description": "Exploration & production.",
        "keywords": ["ongc ", "oil india",
                     "exploration", "e&p", "upstream", "crude oil", "natural gas exploration"],
    },
    {
        "slug": "downstream-refining",
        "name": "Refining & Marketing",
        "parent_sector": "Oil, Gas & Energy",
        "description": "Refineries and oil marketing companies.",
        "keywords": ["reliance industries", "indian oil", "bharat petroleum",
                     "hindustan petroleum", "mangalore refinery", "chennai petroleum",
                     "refining", "refinery", "petroleum", "oil & gas",
                     "oil and gas", "fuels", "marketing", "o2c", "oil to chemicals"],
    },
    {
        "slug": "city-gas-distribution",
        "name": "City Gas Distribution",
        "parent_sector": "Oil, Gas & Energy",
        "description": "CGD players.",
        "keywords": ["indraprastha gas", "mahanagar gas", "gujarat gas", "gail ",
                     "petronet lng", "adani total gas",
                     "city gas", "natural gas", "lng", "cng", "gas distribution",
                     "piped natural gas"],
    },

    # ── Power & Utilities ───────────────────────────────────────
    {
        "slug": "power-generation",
        "name": "Power Generation",
        "parent_sector": "Power & Utilities",
        "description": "Thermal, hydro and nuclear gencos.",
        "keywords": ["ntpc ", "tata power", "jsw energy", "adani power", "torrent power",
                     "nhpc ", "sjvn ",
                     "power generation", "power ", "thermal power", "thermal",
                     "hydro power", "hydroelectric", "coal-based power", "generation",
                     "electricity generation"],
    },
    {
        "slug": "power-transmission",
        "name": "Power Transmission",
        "parent_sector": "Power & Utilities",
        "description": "Transmission and grid.",
        "keywords": ["power grid", "adani energy solutions", "torrent power transmission",
                     "transmission", "power transmission", "distribution",
                     "power distribution", "grid"],
    },
    {
        "slug": "renewables",
        "name": "Renewables",
        "parent_sector": "Power & Utilities",
        "description": "Solar, wind and green-energy players.",
        "keywords": ["adani green", "suzlon", "waaree renewable", "inox wind",
                     "borosil renewables",
                     "renewable", "renewables", "solar", "solar power", "wind",
                     "wind power", "green energy", "clean energy"],
    },

    # ── FMCG & Consumer ─────────────────────────────────────────
    {
        "slug": "fmcg-staples",
        "name": "FMCG Staples",
        "parent_sector": "FMCG & Consumer",
        "description": "Packaged staples and personal care.",
        "keywords": ["hindustan unilever", "itc ", "nestle india", "britannia",
                     "dabur india", "marico", "godrej consumer", "colgate-palmolive",
                     "tata consumer", "emami",
                     "fmcg", "consumer products", "consumer goods", "packaged food",
                     "personal care", "home care", "dairy", "foods", "beverages",
                     "cigarettes", "tobacco"],
    },
    {
        "slug": "consumer-durables",
        "name": "Consumer Durables",
        "parent_sector": "FMCG & Consumer",
        "description": "White goods, kitchen appliances, paints.",
        "keywords": ["asian paints", "berger paints", "kansai nerolac", "whirlpool",
                     "havells", "voltas", "crompton greaves consumer", "bajaj electricals",
                     "kajaria ceramics", "dixon technologies",
                     "durables", "appliances", "paints", "electricals", "electronics",
                     "consumer electronics", "white goods", "kitchen appliances",
                     "lighting", "wires and cables"],
    },
    {
        "slug": "qsr-retail",
        "name": "QSR & Retail",
        "parent_sector": "FMCG & Consumer",
        "description": "Quick-service restaurants, multi-format retail.",
        "keywords": ["jubilant foodworks", "westside", "trent ", "aditya birla fashion",
                     "avenue supermarts", "dmart", "metro brands", "relaxo footwears",
                     "bata india",
                     "retail ", "qsr", "restaurants", "food services", "apparel retail",
                     "footwear", "jewellery", "jewelry", "hospitality", "hotel", "hotels"],
    },

    # ── Telecom & Media ─────────────────────────────────────────
    {
        "slug": "telecom-services",
        "name": "Telecom Services",
        "parent_sector": "Telecom & Media",
        "description": "Mobile and fixed-line telecom services.",
        "keywords": ["bharti airtel", "vodafone idea", "tata communications",
                     "tata teleservices",
                     "telecom", "telecommunications", "wireless", "mobility",
                     "voice services", "broadband", "fixed line"],
    },
    {
        "slug": "media-broadcasting",
        "name": "Media & Broadcasting",
        "parent_sector": "Telecom & Media",
        "description": "TV, print and digital media.",
        "keywords": ["zee entertainment", "sun tv", "pvr inox", "network18", "tv18",
                     "dish tv", "saregama",
                     "broadcasting", "television", "entertainment", "media",
                     "content", "print media", "radio", "cinema", "film"],
    },

    # ── Defence & Aerospace ─────────────────────────────────────
    {
        "slug": "defence-land-systems",
        "name": "Defence — Land Systems",
        "parent_sector": "Defence & Aerospace",
        "description": "Land-based defence OEMs and ordnance.",
        "keywords": ["bharat electronics", "bharat dynamics", "mishra dhatu",
                     "solar industries",
                     "defence", "defense", "ammunition", "ordnance", "military",
                     "weapons", "explosives"],
    },
    {
        "slug": "shipbuilding",
        "name": "Shipbuilding & Naval",
        "parent_sector": "Defence & Aerospace",
        "description": "Shipyards.",
        "keywords": ["mazagon dock", "garden reach", "cochin shipyard"],
    },
    {
        "slug": "aerospace",
        "name": "Aerospace",
        "parent_sector": "Defence & Aerospace",
        "description": "Aircraft, aerospace components, MRO.",
        "keywords": ["hindustan aeronautics", "hal ", "data patterns", "paras defence",
                     "mtar technologies", "astra microwave",
                     "aerospace", "aviation", "aeronautics", "satellite", "space"],
    },

    # ── Railways & Logistics ────────────────────────────────────
    {
        "slug": "railway-psu",
        "name": "Railway PSU",
        "parent_sector": "Railways & Logistics",
        "description": "Government railway entities.",
        "keywords": ["ircon", "rvnl ", "ircon international", "irctc ", "ircf ",
                     "rail vikas", "rail india", "railtel", "container corporation",
                     "railways", "railway", "rail infrastructure", "rolling stock"],
    },
    {
        "slug": "ports",
        "name": "Ports",
        "parent_sector": "Railways & Logistics",
        "description": "Port operators.",
        "keywords": ["adani ports", "gujarat pipavav", "jsw infrastructure",
                     "ports", "port operations", "port services"],
    },
    {
        "slug": "logistics-3pl",
        "name": "Logistics / 3PL",
        "parent_sector": "Railways & Logistics",
        "description": "Third-party logistics, express and e-com fulfilment.",
        "keywords": ["blue dart", "delhivery", "vrl logistics", "tci express",
                     "mahindra logistics", "tvs supply chain", "allcargo",
                     "logistics", "freight", "transportation", "shipping", "cargo",
                     "supply chain", "warehouse", "warehousing", "courier",
                     "express services"],
    },

    # ── Engineering & Capital Goods ─────────────────────────────
    {
        "slug": "heavy-engineering",
        "name": "Heavy Engineering",
        "parent_sector": "Engineering & Capital Goods",
        "description": "Large capital goods and engineering EPC.",
        "keywords": ["larsen & toubro", "siemens", "abb india", "thermax", "cummins india",
                     "honeywell automation", "bharat heavy electricals",
                     "engineering", "heavy engineering", "capital goods",
                     "industrial automation", "power systems", "energy systems"],
    },
    {
        "slug": "industrial-products",
        "name": "Industrial Products",
        "parent_sector": "Engineering & Capital Goods",
        "description": "Bearings, pumps, specialty industrial components.",
        "keywords": ["schaeffler india", "timken india", "skf india", "grindwell norton",
                     "kirloskar pneumatic", "elgi equipments",
                     "industrial", "machinery", "engineering products", "bearings",
                     "pumps", "compressors", "abrasives", "industrial equipment",
                     "manufacturing"],
    },

    # ── Cement & Building Materials ─────────────────────────────
    {
        "slug": "cement",
        "name": "Cement",
        "parent_sector": "Cement & Building Materials",
        "description": "Cement producers.",
        "keywords": ["ultratech cement", "ambuja cement", "acc ", "shree cement",
                     "dalmia bharat", "ramco cements", "jk cement", "jk lakshmi",
                     "birla corporation", "heidelberg cement",
                     "cement", "concrete", "ready mix"],
    },
    {
        "slug": "pipes-fittings",
        "name": "Pipes & Fittings",
        "parent_sector": "Cement & Building Materials",
        "description": "Plastic, CPVC, steel pipes.",
        "keywords": ["astral ", "supreme industries", "finolex industries", "prince pipes",
                     "apollo pipes", "apl apollo tubes",
                     "pipes", "pvc pipes", "cpvc", "plastic pipes", "plumbing"],
    },
    {
        "slug": "ceramics-tiles",
        "name": "Ceramics & Tiles",
        "parent_sector": "Cement & Building Materials",
        "description": "Tiles, ceramics, sanitaryware and building surfaces.",
        "keywords": ["ceramics", "ceramic", "tiles", "tile", "sanitaryware",
                     "sanitary ware", "kajaria", "cera sanitaryware", "somany",
                     "asian granito", "glazed"],
    },

    # ── Agriculture & Allied ────────────────────────────────────
    {
        "slug": "sugar",
        "name": "Sugar",
        "parent_sector": "Agriculture & Allied",
        "description": "Integrated sugar mills and distilleries.",
        "keywords": ["balrampur chini", "bajaj hindusthan", "shree renuka", "dhampur sugar",
                     "eid parry", "triveni engineering",
                     "sugar", "distillery", "distilleries", "ethanol", "molasses"],
    },
    {
        "slug": "edible-oils",
        "name": "Edible Oils & Agri Processing",
        "parent_sector": "Agriculture & Allied",
        "description": "Oilseed processors, agri-processing.",
        "keywords": ["adani wilmar", "patanjali foods", "kse ", "gokul agro",
                     "ruchi soya",
                     "edible oil", "edible oils", "agri processing", "agro processing",
                     "oilseeds", "vegetable oils"],
    },
    {
        "slug": "tea-coffee",
        "name": "Tea & Coffee",
        "parent_sector": "Agriculture & Allied",
        "description": "Plantations and branded tea/coffee.",
        "keywords": ["tata consumer", "ccl products", "mcleod russel", "bombay burmah",
                     "plantations", "plantation", "tea", "coffee", "rubber plantation"],
    },

    # ── Textiles & Apparel ──────────────────────────────────────
    {
        "slug": "textiles",
        "name": "Textiles & Yarn",
        "parent_sector": "Textiles & Apparel",
        "description": "Integrated textile mills, yarn and fabric producers.",
        "keywords": ["textiles", "textile", "yarn", "fabric", "cotton yarn",
                     "spinning", "weaving", "denim", "knitwear", "home textiles"],
    },
    {
        "slug": "apparel-garments",
        "name": "Apparel & Garments",
        "parent_sector": "Textiles & Apparel",
        "description": "Branded apparel and garment exporters.",
        "keywords": ["garments", "apparel", "clothing", "readymade garments",
                     "arvind fashions", "gokaldas exports", "page industries",
                     "kpr mill", "welspun india"],
    },

    # ── Miscellaneous ───────────────────────────────────────────
    {
        "slug": "diversified-holdings",
        "name": "Diversified Holdings",
        "parent_sector": "Miscellaneous",
        "description": "Holding companies and diversified conglomerates with no clear primary segment.",
        "keywords": ["diversified", "conglomerate", "holding company", "holdings",
                     "trading", "investment"],
    },
]


# ──────────────────────────────────────────────────────────────
# Convenience: index by slug / parent_sector.
# ──────────────────────────────────────────────────────────────
def by_slug():
    return {e["slug"]: e for e in CUSTOM_SECTORS_SEED}


def by_parent():
    out = {}
    for e in CUSTOM_SECTORS_SEED:
        out.setdefault(e["parent_sector"], []).append(e)
    return out
