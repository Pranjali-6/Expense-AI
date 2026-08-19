"""Reference data: the category tree and the merchant dictionary.

This is the deterministic knowledge the system starts with. Everything here is
what lets a fresh install categorise an Indian bank statement correctly without
an AI call — which is the whole reason the platform still works with
``AI_ENABLED=false``.

``color`` holds a **palette slot name**, not a hex. The frontend resolves it to
a light or dark step through ``lib/palette.ts``, so the two themes stay in sync
from one definition. Twenty-two categories share eight hues because the colour
sits beside the category's name everywhere it appears; it is identity, never the
sole carrier of meaning. Chart series colours are a separate concern with a hard
eight-series cap.

Merchant names are real Indian businesses — that is the point of a merchant
dictionary — but no transaction, person or account here is real.
"""

from __future__ import annotations

from typing import TypedDict


class SubcategorySeed(TypedDict):
    slug: str
    name: str


class CategorySeed(TypedDict):
    slug: str
    name: str
    is_expense: bool
    is_income: bool
    color: str
    icon: str
    subcategories: list[SubcategorySeed]


def _sub(slug: str, name: str) -> SubcategorySeed:
    return {"slug": slug, "name": name}


# --------------------------------------------------------------------------- #
# Categories
#
# `is_expense` is the flag every analytics query filters on. Getting it wrong is
# how personal finance tools end up claiming someone spent twice their income:
# a transfer between your own accounts is not spending, and neither is the
# credit-card payment that settles purchases already counted individually.
# --------------------------------------------------------------------------- #

CATEGORIES: list[CategorySeed] = [
    {
        "slug": "food", "name": "Food", "is_expense": True, "is_income": False,
        "color": "orange", "icon": "utensils",
        "subcategories": [
            _sub("restaurants", "Restaurants"),
            _sub("food_delivery", "Food Delivery"),
            _sub("cafes", "Cafés"),
            _sub("street_food", "Street Food"),
            _sub("bakery", "Bakery & Sweets"),
        ],
    },
    {
        "slug": "grocery", "name": "Grocery", "is_expense": True, "is_income": False,
        "color": "aqua", "icon": "shopping-basket",
        "subcategories": [
            _sub("supermarket", "Supermarket"),
            _sub("quick_commerce", "Quick Commerce"),
            _sub("kirana", "Kirana Store"),
            _sub("meat_seafood", "Meat & Seafood"),
            _sub("dairy", "Dairy"),
        ],
    },
    {
        "slug": "rent", "name": "Rent", "is_expense": True, "is_income": False,
        "color": "violet", "icon": "home",
        "subcategories": [
            _sub("house_rent", "House Rent"),
            _sub("maintenance", "Society Maintenance"),
            _sub("brokerage", "Brokerage"),
            _sub("deposit", "Security Deposit"),
        ],
    },
    {
        "slug": "utilities", "name": "Utilities", "is_expense": True, "is_income": False,
        "color": "blue", "icon": "zap",
        "subcategories": [
            _sub("electricity", "Electricity"),
            _sub("water", "Water"),
            _sub("gas", "Gas & LPG"),
            _sub("internet", "Internet & Broadband"),
            _sub("mobile", "Mobile & DTH"),
        ],
    },
    {
        "slug": "shopping", "name": "Shopping", "is_expense": True, "is_income": False,
        "color": "magenta", "icon": "shopping-bag",
        "subcategories": [
            _sub("clothing", "Clothing & Footwear"),
            _sub("electronics", "Electronics"),
            _sub("home_furnishing", "Home & Furnishing"),
            _sub("personal_care", "Personal Care & Beauty"),
            _sub("gifts", "Gifts"),
        ],
    },
    {
        "slug": "travel", "name": "Travel", "is_expense": True, "is_income": False,
        "color": "blue", "icon": "plane",
        "subcategories": [
            _sub("flights", "Flights"),
            _sub("trains", "Trains"),
            _sub("hotels", "Hotels & Stays"),
            _sub("cabs", "Cabs & Ride-hailing"),
            _sub("bus", "Bus"),
            _sub("tolls_parking", "Tolls & Parking"),
        ],
    },
    {
        "slug": "fuel", "name": "Fuel", "is_expense": True, "is_income": False,
        "color": "yellow", "icon": "fuel",
        "subcategories": [
            _sub("petrol", "Petrol"),
            _sub("diesel", "Diesel"),
            _sub("cng", "CNG"),
            _sub("ev_charging", "EV Charging"),
        ],
    },
    {
        "slug": "entertainment", "name": "Entertainment", "is_expense": True,
        "is_income": False, "color": "magenta", "icon": "clapperboard",
        "subcategories": [
            _sub("movies", "Movies"),
            _sub("events", "Events & Concerts"),
            _sub("gaming", "Gaming"),
            _sub("books_media", "Books & Media"),
            _sub("hobbies", "Hobbies"),
        ],
    },
    {
        "slug": "subscriptions", "name": "Subscriptions", "is_expense": True,
        "is_income": False, "color": "violet", "icon": "repeat",
        "subcategories": [
            _sub("streaming", "Streaming"),
            _sub("software", "Software & Cloud"),
            _sub("news", "News & Reading"),
            _sub("fitness", "Fitness"),
            _sub("memberships", "Memberships"),
        ],
    },
    {
        "slug": "healthcare", "name": "Healthcare", "is_expense": True, "is_income": False,
        "color": "red", "icon": "heart-pulse",
        "subcategories": [
            _sub("pharmacy", "Pharmacy"),
            _sub("doctor", "Doctor Consultation"),
            _sub("diagnostics", "Diagnostics & Labs"),
            _sub("hospital", "Hospital"),
            _sub("dental_vision", "Dental & Vision"),
        ],
    },
    {
        "slug": "insurance", "name": "Insurance", "is_expense": True, "is_income": False,
        "color": "red", "icon": "shield",
        "subcategories": [
            _sub("health_insurance", "Health"),
            _sub("life_insurance", "Life"),
            _sub("motor_insurance", "Motor"),
            _sub("home_insurance", "Home"),
        ],
    },
    {
        "slug": "education", "name": "Education", "is_expense": True, "is_income": False,
        "color": "aqua", "icon": "graduation-cap",
        "subcategories": [
            _sub("tuition", "School & Tuition Fees"),
            _sub("online_courses", "Online Courses"),
            _sub("books_supplies", "Books & Supplies"),
            _sub("exam_fees", "Exam Fees"),
        ],
    },
    {
        "slug": "emi", "name": "EMI", "is_expense": True, "is_income": False,
        "color": "red", "icon": "landmark",
        "subcategories": [
            _sub("home_loan", "Home Loan"),
            _sub("personal_loan", "Personal Loan"),
            _sub("auto_loan", "Auto Loan"),
            _sub("consumer_durable", "Consumer Durable"),
            _sub("education_loan", "Education Loan"),
        ],
    },
    {
        # Money moved into an asset you still own. Not consumption.
        "slug": "investment", "name": "Investment", "is_expense": False, "is_income": False,
        "color": "green", "icon": "trending-up",
        "subcategories": [
            _sub("mutual_funds", "Mutual Funds & SIP"),
            _sub("stocks", "Stocks"),
            _sub("ppf_epf", "PPF & EPF"),
            _sub("nps", "NPS"),
            _sub("fixed_deposit", "Fixed Deposit"),
            _sub("gold", "Gold"),
        ],
    },
    {
        "slug": "salary", "name": "Salary", "is_expense": False, "is_income": True,
        "color": "green", "icon": "wallet",
        "subcategories": [
            _sub("salary_credit", "Salary Credit"),
            _sub("bonus", "Bonus & Incentive"),
            _sub("reimbursement", "Reimbursement"),
            _sub("freelance", "Freelance & Consulting"),
        ],
    },
    {
        "slug": "bank_charges", "name": "Bank Charges", "is_expense": True,
        "is_income": False, "color": "yellow", "icon": "receipt",
        "subcategories": [
            _sub("account_fees", "Account & Card Fees"),
            _sub("penalties", "Penalties & Late Fees"),
            _sub("forex_markup", "Forex Markup"),
            _sub("atm_charges", "ATM Charges"),
            _sub("interest_charges", "Interest Charges"),
        ],
    },
    {
        "slug": "taxes", "name": "Taxes", "is_expense": True, "is_income": False,
        "color": "yellow", "icon": "file-text",
        "subcategories": [
            _sub("income_tax", "Income Tax"),
            _sub("gst", "GST"),
            _sub("property_tax", "Property Tax"),
            _sub("tds", "TDS"),
        ],
    },
    {
        # NOTE — a deliberate trade-off worth knowing about.
        #
        # Classified as a non-expense movement per the architecture: withdrawing
        # cash moves money to a wallet the statement cannot see, it does not
        # consume it.
        #
        # The cost is that cash spending becomes invisible to totals. The usual
        # argument for excluding a movement — that counting it double-counts the
        # spend it settles — does not really apply here, because we never see the
        # cash spend at all. Worth revisiting once real usage shows how much cash
        # a typical user runs through.
        "slug": "cash_withdrawal", "name": "Cash Withdrawal", "is_expense": False,
        "is_income": False, "color": "orange", "icon": "banknote",
        "subcategories": [
            _sub("atm", "ATM Withdrawal"),
            _sub("branch", "Branch Withdrawal"),
            _sub("cash_back", "Cash at POS"),
        ],
    },
    {
        "slug": "transfers", "name": "Transfers", "is_expense": False, "is_income": False,
        "color": "blue", "icon": "arrow-left-right",
        "subcategories": [
            _sub("self_transfer", "Between Own Accounts"),
            _sub("family_transfer", "To Family & Friends"),
            _sub("received", "Received"),
        ],
    },
    {
        # Settles purchases that were already counted individually. Counting it
        # again is the single most common double-count in personal finance apps.
        "slug": "credit_card_payment", "name": "Credit Card Payment",
        "is_expense": False, "is_income": False, "color": "violet", "icon": "credit-card",
        "subcategories": [
            _sub("card_bill", "Card Bill Payment"),
            _sub("autopay", "Autopay"),
        ],
    },
    {
        "slug": "refund", "name": "Refund", "is_expense": False, "is_income": False,
        "color": "green", "icon": "undo-2",
        "subcategories": [
            _sub("purchase_refund", "Purchase Refund"),
            _sub("cancellation", "Cancellation Refund"),
            _sub("cashback", "Cashback"),
            _sub("reversal", "Failed Transaction Reversal"),
        ],
    },
    {
        "slug": "other", "name": "Other", "is_expense": True, "is_income": False,
        "color": "neutral", "icon": "circle-help",
        "subcategories": [_sub("uncategorised", "Uncategorised")],
    },
]


# --------------------------------------------------------------------------- #
# Merchant dictionary
#
# Aliases are matched against a description that has already had UPI handles,
# reference numbers and terminal ids stripped. They are deliberately generous:
# an over-broad alias produces a wrong category the user corrects once, while a
# missing alias produces an AI call or a review-queue entry every single time.
# --------------------------------------------------------------------------- #

class MerchantSeed(TypedDict):
    slug: str
    name: str
    category: str
    subcategory: str | None
    aliases: list[str]
    subscription: bool
    mcc: str | None


def _m(
    slug: str,
    name: str,
    category: str,
    subcategory: str | None,
    aliases: list[str],
    *,
    subscription: bool = False,
    mcc: str | None = None,
) -> MerchantSeed:
    return {
        "slug": slug,
        "name": name,
        "category": category,
        "subcategory": subcategory,
        "aliases": aliases,
        "subscription": subscription,
        "mcc": mcc,
    }


MERCHANTS: list[MerchantSeed] = [
    # --- food delivery & restaurants ---------------------------------------
    _m("swiggy", "Swiggy", "food", "food_delivery",
       ["SWIGGY", "SWIGGYIN", "SWIGGY ORDER", "BUNDL TECHNOLOGIES"], mcc="5812"),
    _m("zomato", "Zomato", "food", "food_delivery",
       ["ZOMATO", "ZOMATO ONLINE", "ZOMATO LTD"], mcc="5812"),
    _m("eatsure", "EatSure", "food", "food_delivery", ["EATSURE", "REBEL FOODS"]),
    _m("dominos", "Domino's Pizza", "food", "restaurants",
       ["DOMINOS", "DOMINO S", "JUBILANT FOODWORKS"], mcc="5812"),
    _m("mcdonalds", "McDonald's", "food", "restaurants",
       ["MCDONALD", "MCDONALDS", "HARDCASTLE RESTAURANTS"], mcc="5814"),
    _m("kfc", "KFC", "food", "restaurants", ["KFC", "DEVYANI INTERNATIONAL"], mcc="5814"),
    _m("burger_king", "Burger King", "food", "restaurants", ["BURGER KING", "BURGERKING"]),
    _m("pizza_hut", "Pizza Hut", "food", "restaurants", ["PIZZA HUT", "PIZZAHUT"]),
    _m("starbucks", "Starbucks", "food", "cafes", ["STARBUCKS", "TATA STARBUCKS"], mcc="5814"),
    _m("cafe_coffee_day", "Café Coffee Day", "food", "cafes", ["CAFE COFFEE DAY", "CCD "]),
    _m("third_wave", "Third Wave Coffee", "food", "cafes", ["THIRD WAVE", "THIRDWAVE"]),
    _m("chaayos", "Chaayos", "food", "cafes", ["CHAAYOS", "SUNSHINE TEAHOUSE"]),
    _m("blue_tokai", "Blue Tokai", "food", "cafes", ["BLUE TOKAI", "BLUETOKAI"]),
    _m("haldiram", "Haldiram's", "food", "bakery", ["HALDIRAM", "HALDIRAMS"]),
    _m("barbeque_nation", "Barbeque Nation", "food", "restaurants", ["BARBEQUE NATION", "BBQ NATION"]),

    # --- grocery & quick commerce ------------------------------------------
    _m("blinkit", "Blinkit", "grocery", "quick_commerce",
       ["BLINKIT", "GROFERS", "HANDS ON TRADES"], mcc="5411"),
    _m("zepto", "Zepto", "grocery", "quick_commerce", ["ZEPTO", "KIRANAKART"], mcc="5411"),
    _m("instamart", "Swiggy Instamart", "grocery", "quick_commerce",
       ["INSTAMART", "SWIGGYINSTAMART", "SWIGGY INSTAMART"], mcc="5411"),
    _m("bigbasket", "BigBasket", "grocery", "supermarket",
       ["BIGBASKET", "BIG BASKET", "INNOVATIVE RETAIL"], mcc="5411"),
    _m("dmart", "DMart", "grocery", "supermarket",
       ["DMART", "D MART", "AVENUE SUPERMARTS"], mcc="5411"),
    _m("reliance_fresh", "Reliance Fresh", "grocery", "supermarket",
       ["RELIANCE FRESH", "RELIANCE SMART", "RELIANCE RETAIL"]),
    _m("jiomart", "JioMart", "grocery", "supermarket", ["JIOMART", "JIO MART"]),
    _m("more_retail", "More Supermarket", "grocery", "supermarket", ["MORE RETAIL", "MORE SUPERMARKET"]),
    _m("licious", "Licious", "grocery", "meat_seafood", ["LICIOUS", "DELIGHTFUL GOURMET"]),
    _m("country_delight", "Country Delight", "grocery", "dairy",
       ["COUNTRY DELIGHT", "COUNTRYDELIGHT"], subscription=True),

    # --- e-commerce ---------------------------------------------------------
    _m("amazon", "Amazon", "shopping", None,
       ["AMAZON", "AMZN", "AMAZON PAY", "AMAZON SELLER", "AMAZON IN"], mcc="5399"),
    _m("flipkart", "Flipkart", "shopping", None,
       ["FLIPKART", "FKRT", "FLIPKART INTERNET"], mcc="5399"),
    _m("myntra", "Myntra", "shopping", "clothing", ["MYNTRA", "MYNTRA DESIGNS"], mcc="5651"),
    _m("ajio", "AJIO", "shopping", "clothing", ["AJIO", "RELIANCE AJIO"]),
    _m("nykaa", "Nykaa", "shopping", "personal_care", ["NYKAA", "FSN E COMMERCE"], mcc="5977"),
    _m("meesho", "Meesho", "shopping", None, ["MEESHO", "FASHNEAR TECHNOLOGIES"]),
    _m("tata_cliq", "Tata CLiQ", "shopping", None, ["TATA CLIQ", "TATACLIQ"]),
    _m("decathlon", "Decathlon", "shopping", None, ["DECATHLON"]),
    _m("ikea", "IKEA", "shopping", "home_furnishing", ["IKEA"]),
    _m("croma", "Croma", "shopping", "electronics", ["CROMA", "INFINITI RETAIL"]),
    _m("reliance_digital", "Reliance Digital", "shopping", "electronics", ["RELIANCE DIGITAL"]),
    _m("apple_store", "Apple", "shopping", "electronics", ["APPLE STORE", "APPLE INDIA"]),

    # --- travel -------------------------------------------------------------
    _m("irctc", "IRCTC", "travel", "trains", ["IRCTC", "INDIAN RAILWAY", "IRCTC UTS"], mcc="4112"),
    _m("makemytrip", "MakeMyTrip", "travel", None, ["MAKEMYTRIP", "MMT ", "MAKE MY TRIP"], mcc="4722"),
    _m("goibibo", "Goibibo", "travel", None, ["GOIBIBO", "IBIBO"]),
    _m("cleartrip", "Cleartrip", "travel", None, ["CLEARTRIP"]),
    _m("ixigo", "ixigo", "travel", None, ["IXIGO", "LE TRAVENUES"]),
    _m("uber", "Uber", "travel", "cabs", ["UBER", "UBER INDIA", "UBER RIDES"], mcc="4121"),
    _m("ola", "Ola", "travel", "cabs", ["OLA ", "OLACABS", "ANI TECHNOLOGIES"], mcc="4121"),
    _m("rapido", "Rapido", "travel", "cabs", ["RAPIDO", "ROPPEN TRANSPORTATION"]),
    _m("indigo", "IndiGo", "travel", "flights", ["INDIGO", "INTERGLOBE AVIATION", "GOINDIGO"], mcc="3260"),
    _m("air_india", "Air India", "travel", "flights", ["AIR INDIA", "AIRINDIA"], mcc="3000"),
    _m("akasa_air", "Akasa Air", "travel", "flights", ["AKASA", "SNV AVIATION"]),
    _m("oyo", "OYO", "travel", "hotels", ["OYO", "OYO ROOMS", "ORAVEL STAYS"], mcc="7011"),
    _m("fastag", "FASTag", "travel", "tolls_parking", ["FASTAG", "NETC", "NHAI"]),

    # --- fuel ---------------------------------------------------------------
    _m("indian_oil", "Indian Oil", "fuel", "petrol", ["INDIAN OIL", "IOCL", "INDIANOIL"], mcc="5541"),
    _m("hp_petrol", "HP Petrol Pump", "fuel", "petrol",
       ["HINDUSTAN PETROLEUM", "HPCL", "HP PETROL"], mcc="5541"),
    _m("bharat_petroleum", "Bharat Petroleum", "fuel", "petrol", ["BHARAT PETROLEUM", "BPCL"], mcc="5541"),
    _m("shell", "Shell", "fuel", "petrol", ["SHELL INDIA", "SHELL PETROL"]),
    _m("nayara", "Nayara Energy", "fuel", "petrol", ["NAYARA", "ESSAR OIL"]),

    # --- entertainment & streaming -----------------------------------------
    _m("bookmyshow", "BookMyShow", "entertainment", "movies",
       ["BOOKMYSHOW", "BIGTREE ENTERTAINMENT", "BMS "], mcc="7832"),
    _m("pvr_inox", "PVR INOX", "entertainment", "movies", ["PVR", "INOX LEISURE", "PVR INOX"], mcc="7832"),
    _m("netflix", "Netflix", "subscriptions", "streaming",
       ["NETFLIX", "NETFLIX COM"], subscription=True, mcc="4899"),
    _m("prime_video", "Amazon Prime", "subscriptions", "streaming",
       ["PRIME VIDEO", "AMAZON PRIME", "AMAZONPRIME"], subscription=True),
    _m("hotstar", "Disney+ Hotstar", "subscriptions", "streaming",
       ["HOTSTAR", "DISNEY", "NOVI DIGITAL"], subscription=True),
    _m("spotify", "Spotify", "subscriptions", "streaming",
       ["SPOTIFY"], subscription=True, mcc="4899"),
    _m("jiosaavn", "JioSaavn", "subscriptions", "streaming", ["JIOSAAVN", "SAAVN"], subscription=True),
    _m("youtube_premium", "YouTube Premium", "subscriptions", "streaming",
       ["YOUTUBE", "GOOGLE YOUTUBE"], subscription=True),
    _m("sony_liv", "SonyLIV", "subscriptions", "streaming", ["SONYLIV", "SONY LIV"], subscription=True),
    _m("zee5", "ZEE5", "subscriptions", "streaming", ["ZEE5", "ZEE ENTERTAINMENT"], subscription=True),
    _m("cultfit", "cult.fit", "subscriptions", "fitness",
       ["CULTFIT", "CULT FIT", "CUREFIT"], subscription=True),

    # --- software & cloud ---------------------------------------------------
    _m("google_services", "Google", "subscriptions", "software",
       ["GOOGLE ", "GOOGLE PAYMENT", "GOOGLE CLOUD", "GOOGLE ONE"], subscription=True),
    _m("microsoft", "Microsoft", "subscriptions", "software",
       ["MICROSOFT", "MSFT", "MICROSOFT 365"], subscription=True),
    _m("adobe", "Adobe", "subscriptions", "software", ["ADOBE"], subscription=True),
    _m("apple_services", "Apple Services", "subscriptions", "software",
       ["APPLE COM BILL", "ITUNES", "APPLE SERVICES"], subscription=True),
    _m("openai", "OpenAI", "subscriptions", "software", ["OPENAI", "CHATGPT"], subscription=True),
    _m("anthropic", "Anthropic", "subscriptions", "software", ["ANTHROPIC", "CLAUDE AI"], subscription=True),
    _m("github", "GitHub", "subscriptions", "software", ["GITHUB"], subscription=True),
    _m("notion", "Notion", "subscriptions", "software", ["NOTION LABS", "NOTION SO"], subscription=True),
    _m("figma", "Figma", "subscriptions", "software", ["FIGMA"], subscription=True),
    _m("aws", "Amazon Web Services", "subscriptions", "software", ["AWS ", "AMAZON WEB SERVICES"], subscription=True),

    # --- telecom & utilities ------------------------------------------------
    _m("airtel", "Airtel", "utilities", "mobile",
       ["AIRTEL", "BHARTI AIRTEL", "AIRTEL PAYMENTS"], mcc="4814"),
    _m("jio", "Jio", "utilities", "mobile",
       ["RELIANCE JIO", "JIO RECHARGE", "JIO PREPAID"], mcc="4814"),
    _m("vodafone_idea", "Vi", "utilities", "mobile", ["VODAFONE", "VODAFONE IDEA", "VI RECHARGE"]),
    _m("bsnl", "BSNL", "utilities", "mobile", ["BSNL"]),
    _m("act_fibernet", "ACT Fibernet", "utilities", "internet",
       ["ACT FIBERNET", "ATRIA CONVERGENCE"], subscription=True),
    _m("tata_power", "Tata Power", "utilities", "electricity", ["TATA POWER", "TPDDL"], mcc="4900"),
    _m("adani_electricity", "Adani Electricity", "utilities", "electricity", ["ADANI ELECTRICITY", "AEML"]),
    _m("bescom", "BESCOM", "utilities", "electricity", ["BESCOM", "BANGALORE ELECTRICITY"]),
    _m("mahadiscom", "MSEDCL", "utilities", "electricity", ["MAHADISCOM", "MSEDCL"]),
    _m("indane_gas", "Indane Gas", "utilities", "gas", ["INDANE", "INDIAN OIL LPG"]),

    # --- healthcare ---------------------------------------------------------
    _m("apollo_pharmacy", "Apollo Pharmacy", "healthcare", "pharmacy",
       ["APOLLO PHARMACY", "APOLLO HOSPITALS"], mcc="5912"),
    _m("pharmeasy", "PharmEasy", "healthcare", "pharmacy",
       ["PHARMEASY", "API HOLDINGS", "MEDLIFE"], mcc="5912"),
    _m("tata_1mg", "Tata 1mg", "healthcare", "pharmacy", ["1MG", "TATA 1MG", "ONEMG"]),
    _m("netmeds", "Netmeds", "healthcare", "pharmacy", ["NETMEDS"]),
    _m("practo", "Practo", "healthcare", "doctor", ["PRACTO"]),
    _m("dr_lal_pathlabs", "Dr Lal PathLabs", "healthcare", "diagnostics",
       ["LAL PATHLABS", "DR LAL", "LALPATH"], mcc="8071"),
    _m("metropolis", "Metropolis Healthcare", "healthcare", "diagnostics", ["METROPOLIS"]),

    # --- investment & broking ----------------------------------------------
    _m("zerodha", "Zerodha", "investment", "stocks", ["ZERODHA", "ZERODHA BROKING"], mcc="6211"),
    _m("groww", "Groww", "investment", "mutual_funds", ["GROWW", "NEXTBILLION TECHNOLOGY"]),
    _m("upstox", "Upstox", "investment", "stocks", ["UPSTOX", "RKSV SECURITIES"]),
    _m("kuvera", "Kuvera", "investment", "mutual_funds", ["KUVERA"]),
    _m("et_money", "ET Money", "investment", "mutual_funds", ["ET MONEY", "ETMONEY"]),
    _m("nps_trust", "NPS", "investment", "nps", ["NPS TRUST", "NSDL NPS", "PROTEAN NPS"]),
    _m("mf_utilities", "MF Utilities", "investment", "mutual_funds",
       ["MF UTILITIES", "MFU ", "BSE STARMF", "NSE MFSS"]),

    # --- insurance ----------------------------------------------------------
    _m("lic", "LIC", "insurance", "life_insurance", ["LIC OF INDIA", "LICI", "LIFE INSURANCE CORP"]),
    _m("hdfc_ergo", "HDFC ERGO", "insurance", "motor_insurance", ["HDFC ERGO"]),
    _m("icici_lombard", "ICICI Lombard", "insurance", "motor_insurance", ["ICICI LOMBARD"]),
    _m("star_health", "Star Health", "insurance", "health_insurance", ["STAR HEALTH"]),
    _m("bajaj_allianz", "Bajaj Allianz", "insurance", "health_insurance", ["BAJAJ ALLIANZ"]),
    _m("policybazaar", "Policybazaar", "insurance", None, ["POLICYBAZAAR", "PB FINTECH"]),

    # --- education ----------------------------------------------------------
    _m("byjus", "BYJU'S", "education", "online_courses", ["BYJUS", "THINK AND LEARN"]),
    _m("unacademy", "Unacademy", "education", "online_courses", ["UNACADEMY", "SORTING HAT"]),
    _m("coursera", "Coursera", "education", "online_courses", ["COURSERA"], subscription=True),
    _m("udemy", "Udemy", "education", "online_courses", ["UDEMY"]),
    _m("physics_wallah", "Physics Wallah", "education", "online_courses", ["PHYSICS WALLAH", "PW "]),

    # --- payments & wallets -------------------------------------------------
    # These are rails, not merchants. Mapped to Other so the categoriser is
    # forced to look at the rest of the description rather than concluding that
    # every UPI payment is a "Paytm expense".
    _m("paytm", "Paytm", "other", "uncategorised", ["PAYTM", "ONE97 COMMUNICATIONS"]),
    _m("phonepe", "PhonePe", "other", "uncategorised", ["PHONEPE", "PHONE PE"]),
    _m("cred", "CRED", "credit_card_payment", "card_bill", ["CRED ", "CRED CLUB", "DREAMPLUG"]),
    _m("razorpay", "Razorpay", "other", "uncategorised", ["RAZORPAY", "RAZOR PAY"]),
    _m("billdesk", "BillDesk", "other", "uncategorised", ["BILLDESK", "BILL DESK"]),
]


# --------------------------------------------------------------------------- #
# Deterministic description rules
#
# Matched against the raw description before merchant normalisation. These carry
# the bank's own vocabulary — the words that identify a movement regardless of
# who the counterparty was.
# --------------------------------------------------------------------------- #

class DescriptionRule(TypedDict):
    pattern: str
    category: str
    subcategory: str | None
    movement_type: str


DESCRIPTION_RULES: list[DescriptionRule] = [
    # Salary
    {"pattern": r"\b(SALARY|SAL\s+CREDIT|SALARY\s+CREDIT|NEFT.*SALARY)\b",
     "category": "salary", "subcategory": "salary_credit", "movement_type": "salary"},

    # ATM / cash
    {"pattern": r"\b(ATM\s*WDL|ATW|CASH\s*WDL|ATM\s*CASH|NWD)\b",
     "category": "cash_withdrawal", "subcategory": "atm", "movement_type": "cash_withdrawal"},

    # Credit card settlement
    {"pattern": r"\b(CREDIT\s*CARD\s*PAY|CC\s*PAYMENT|CARD\s*PAYMENT|AUTOPAY.*CARD)\b",
     "category": "credit_card_payment", "subcategory": "card_bill",
     "movement_type": "credit_card_payment"},

    # Bank charges — a bank's own fees are unmistakable and never a merchant
    {"pattern": r"\b(SMS\s*CHARGES?|AMC|ANNUAL\s*FEE|SERVICE\s*CHARGE|MIN\s*BAL|"
                r"CHQ\s*RETURN|PENAL|LATE\s*FEE|MARKUP|CONV\s*FEE)\b",
     "category": "bank_charges", "subcategory": "account_fees", "movement_type": "bank_charge"},
    {"pattern": r"\b(ATM\s*DECLINE|ATM\s*CHARGE|CASH\s*WDL\s*CHG)\b",
     "category": "bank_charges", "subcategory": "atm_charges", "movement_type": "bank_charge"},

    # Interest received is income, interest charged is a fee
    {"pattern": r"\b(INT\s*PD|INTEREST\s*CREDIT|CREDIT\s*INTEREST|SB\s*INT)\b",
     "category": "salary", "subcategory": None, "movement_type": "income"},

    # Tax
    {"pattern": r"\b(TDS|INCOME\s*TAX|ADVANCE\s*TAX|GST\s*PAY|CBDT|SELF\s*ASSESSMENT)\b",
     "category": "taxes", "subcategory": None, "movement_type": "expense"},

    # EMI
    {"pattern": r"\b(EMI|LOAN\s*REPAY|ACH\s*D.*LOAN|INSTALLMENT|INSTALMENT)\b",
     "category": "emi", "subcategory": None, "movement_type": "emi"},

    # Investment rails
    {"pattern": r"\b(SIP|MUTUAL\s*FUND|MF\s*PURCHASE|NSE|BSE|DEMAT|PPF|NPS)\b",
     "category": "investment", "subcategory": None, "movement_type": "investment"},

    # Reversals and refunds
    {"pattern": r"\b(REVERSAL|REFUND|RETURNED|FAILED\s*TXN|CHARGEBACK|CASHBACK)\b",
     "category": "refund", "subcategory": "reversal", "movement_type": "refund"},

    # Self transfers
    {"pattern": r"\b(SELF|OWN\s*ACCOUNT|TRANSFER\s*TO\s*SELF|FUND\s*TRANSFER)\b",
     "category": "transfers", "subcategory": "self_transfer", "movement_type": "transfer"},

    # Rent
    {"pattern": r"\b(RENT|HOUSE\s*RENT|MAINTENANCE\s*CHARGE|SOCIETY\s*MAINT)\b",
     "category": "rent", "subcategory": "house_rent", "movement_type": "expense"},
]
