"""The whole world: country editions, translated searches and kid words in many languages.

A search phrase can carry a locale tag, e.g. "[pt-BR] menino salvou". Google News and Bing then search that
country's edition in that language, autocomplete looks it up in that language, and anything found is checked
by the AI, which writes the Discord card in English.
"""
from __future__ import annotations

import re

# locale -> (Google News hl, gl, ceid). Bing uses the locale itself as its market (mkt=).
LOCALES: dict[str, tuple[str, str, str]] = {
    "es-MX": ("es-419", "MX", "MX:es-419"), "es-ES": ("es", "ES", "ES:es"), "es-AR": ("es-419", "AR", "AR:es-419"),
    "es-CO": ("es-419", "CO", "CO:es-419"), "pt-BR": ("pt-BR", "BR", "BR:pt-419"), "pt-PT": ("pt-PT", "PT", "PT:pt-150"),
    "fr-FR": ("fr", "FR", "FR:fr"), "de-DE": ("de", "DE", "DE:de"), "it-IT": ("it", "IT", "IT:it"),
    "nl-NL": ("nl", "NL", "NL:nl"), "pl-PL": ("pl", "PL", "PL:pl"), "ro-RO": ("ro", "RO", "RO:ro"),
    "cs-CZ": ("cs", "CZ", "CZ:cs"), "sk-SK": ("sk", "SK", "SK:sk"), "hu-HU": ("hu", "HU", "HU:hu"),
    "el-GR": ("el", "GR", "GR:el"), "bg-BG": ("bg", "BG", "BG:bg"), "sr-RS": ("sr", "RS", "RS:sr"),
    "sl-SI": ("sl", "SI", "SI:sl"), "sv-SE": ("sv", "SE", "SE:sv"), "no-NO": ("no", "NO", "NO:no"),
    "lt-LT": ("lt", "LT", "LT:lt"), "lv-LV": ("lv", "LV", "LV:lv"), "ru-RU": ("ru", "RU", "RU:ru"),
    "uk-UA": ("uk", "UA", "UA:uk"), "tr-TR": ("tr", "TR", "TR:tr"), "he-IL": ("he", "IL", "IL:he"),
    "ar-EG": ("ar", "EG", "EG:ar"), "ar-SA": ("ar", "SA", "SA:ar"), "hi-IN": ("hi", "IN", "IN:hi"),
    "bn-BD": ("bn", "BD", "BD:bn"), "ta-IN": ("ta", "IN", "IN:ta"), "te-IN": ("te", "IN", "IN:te"),
    "mr-IN": ("mr", "IN", "IN:mr"), "ml-IN": ("ml", "IN", "IN:ml"), "th-TH": ("th", "TH", "TH:th"),
    "vi-VN": ("vi", "VN", "VN:vi"), "id-ID": ("id", "ID", "ID:id"), "zh-CN": ("zh-CN", "CN", "CN:zh-Hans"),
    "zh-TW": ("zh-TW", "TW", "TW:zh-Hant"), "ja-JP": ("ja", "JP", "JP:ja"), "ko-KR": ("ko", "KR", "KR:ko"),
}

# language -> the country editions it is searched in
LANG_LOCALES: dict[str, list[str]] = {
    "es": ["es-MX", "es-ES", "es-AR", "es-CO"], "pt": ["pt-BR", "pt-PT"], "ar": ["ar-EG", "ar-SA"],
    "zh-Hans": ["zh-CN"], "zh-Hant": ["zh-TW"],
}

# Translated searches (kid saves someone, survives, gets punished, rare condition, invents, is bullied...)
WORLD_QUERIES: dict[str, list[str]] = {
    "es": ["niño salvó", "niña salvó", "niño sobrevivió", "estudiante arrestado", "alumno expulsado por",
           "niña enfermedad rara", "niño inventó", "estudiante acosado", "niño encontrado con vida", "niño héroe"],
    "pt": ["menino salvou", "menina salvou", "criança sobreviveu", "estudante preso", "aluno suspenso por",
           "menina doença rara", "menino inventou", "aluno sofria bullying", "criança encontrada viva", "menino herói"],
    "fr": ["un garçon a sauvé", "une fillette a sauvé", "enfant a survécu", "élève arrêté", "élève exclu pour",
           "maladie rare enfant", "garçon a inventé", "élève harcelé", "enfant retrouvé vivant", "petit héros"],
    "de": ["Junge rettet", "Mädchen rettet", "Kind überlebt", "Schüler festgenommen", "Schüler suspendiert",
           "seltene Krankheit Kind", "Junge erfindet", "Schüler gemobbt", "Kind lebend gefunden", "kleiner Held"],
    "it": ["bambino salva", "bambina salva", "bambino sopravvissuto", "studente arrestato", "studente sospeso per",
           "malattia rara bambino", "ragazzo inventa", "studente bullizzato", "bambino trovato vivo", "piccolo eroe"],
    "nl": ["jongen redt", "meisje redt", "kind overleeft", "leerling gearresteerd", "leerling geschorst",
           "zeldzame ziekte kind", "jongen vindt uit", "leerling gepest", "kind levend gevonden", "kleine held"],
    "pl": ["chłopiec uratował", "dziewczynka uratowała", "dziecko przeżyło", "uczeń zatrzymany", "uczeń zawieszony",
           "rzadka choroba dziecko", "chłopiec wynalazł", "uczeń prześladowany", "dziecko znalezione żywe",
           "mały bohater"],
    "ro": ["băiat a salvat", "fetiță a salvat", "copil a supraviețuit", "elev arestat", "elev exmatriculat",
           "boală rară copil", "băiat a inventat", "elev agresat", "copil găsit în viață", "micul erou"],
    "cs": ["chlapec zachránil", "dívka zachránila", "dítě přežilo", "žák zadržen", "vzácná nemoc dítě",
           "chlapec vynalezl", "šikanovaný žák", "malý hrdina"],
    "sk": ["chlapec zachránil", "dievča zachránilo", "dieťa prežilo", "vzácna choroba dieťa", "malý hrdina"],
    "hu": ["fiú megmentette", "kislány megmentette", "gyerek túlélte", "diákot letartóztatták",
           "ritka betegség gyerek", "fiú feltalálta", "zaklatott diák", "kis hős"],
    "el": ["αγόρι έσωσε", "κοριτσάκι έσωσε", "παιδί επέζησε", "μαθητής συνελήφθη", "σπάνια ασθένεια παιδί",
           "μικρός ήρωας"],
    "bg": ["момче спаси", "момиче спаси", "дете оцеля", "рядко заболяване дете", "малък герой"],
    "sr": ["dečak spasao", "devojčica spasila", "dete preživelo", "retka bolest dete", "mali heroj"],
    "sl": ["deček rešil", "deklica rešila", "otrok preživel", "redka bolezen otrok", "mali junak"],
    "sv": ["pojke räddade", "flicka räddade", "barn överlevde", "elev gripen", "ovanlig sjukdom barn",
           "pojke uppfann", "mobbad elev", "liten hjälte"],
    "no": ["gutt reddet", "jente reddet", "barn overlevde", "sjelden sykdom barn", "mobbet elev", "liten helt"],
    "lt": ["berniukas išgelbėjo", "mergaitė išgelbėjo", "vaikas išgyveno", "moksleivis sulaikytas",
           "reta liga vaikas", "berniukas išrado", "patyčios mokykloje", "mažasis didvyris", "vaikas rastas gyvas"],
    "lv": ["zēns izglāba", "meitene izglāba", "bērns izdzīvoja", "reta slimība bērns", "mazais varonis"],
    "ru": ["мальчик спас", "девочка спасла", "ребенок выжил", "школьника задержали", "школьника отстранили",
           "редкое заболевание ребенок", "мальчик изобрел", "травля школьника", "ребенка нашли живым",
           "маленький герой"],
    "uk": ["хлопчик врятував", "дівчинка врятувала", "дитина вижила", "школяра затримали",
           "рідкісна хвороба дитина", "хлопчик винайшов", "булінг школяр", "маленький герой"],
    "tr": ["çocuk kurtardı", "küçük kız kurtardı", "çocuk hayatta kaldı", "öğrenci gözaltına alındı",
           "öğrenciye uzaklaştırma", "nadir hastalık çocuk", "çocuk icat etti", "zorbalığa uğrayan öğrenci",
           "çocuk sağ bulundu", "küçük kahraman"],
    "he": ["ילד הציל", "ילדה הצילה", "ילד שרד", "תלמיד נעצר", "מחלה נדירה ילד", "ילד המציא", "בריונות תלמיד",
           "גיבור קטן"],
    "ar": ["طفل ينقذ", "طفلة تنقذ", "نجاة طفل", "اعتقال طالب", "طفل مرض نادر", "طفل يخترع", "تنمر على طالب",
           "العثور على طفل حيا", "بطل صغير"],
    "hi": ["बच्चे ने बचाई जान", "बच्ची ने बचाई जान", "बच्चा जिंदा बचा", "छात्र गिरफ्तार", "दुर्लभ बीमारी बच्चा",
           "बच्चे ने बनाया", "छात्र के साथ बुलिंग", "नन्हा हीरो", "बच्चा जिंदा मिला"],
    "bn": ["শিশু বাঁচাল", "ছেলেটি বাঁচাল", "শিশু বেঁচে গেল", "ছাত্র গ্রেপ্তার", "বিরল রোগ শিশু", "খুদে নায়ক"],
    "ta": ["சிறுவன் காப்பாற்றினான்", "சிறுமி காப்பாற்றினாள்", "குழந்தை உயிர் தப்பியது", "அரிய நோய் குழந்தை"],
    "te": ["బాలుడు కాపాడాడు", "బాలిక కాపాడింది", "చిన్నారి ప్రాణాలతో", "అరుదైన వ్యాధి చిన్నారి"],
    "mr": ["मुलाने वाचवले", "मुलीने वाचवले", "मूल बचावले", "दुर्मिळ आजार मूल"],
    "ml": ["കുട്ടി രക്ഷിച്ചു", "ബാലൻ രക്ഷിച്ചു", "അപൂർവ രോഗം കുട്ടി"],
    "th": ["เด็กชายช่วยชีวิต", "เด็กหญิงช่วยชีวิต", "เด็กรอดชีวิต", "นักเรียนถูกจับ", "โรคหายาก เด็ก",
           "เด็กประดิษฐ์", "นักเรียนถูกบูลลี่", "ฮีโร่ตัวน้อย"],
    "vi": ["bé trai cứu", "bé gái cứu", "cháu bé sống sót", "học sinh bị bắt", "học sinh bị đình chỉ",
           "bệnh hiếm gặp bé", "cậu bé chế tạo", "học sinh bị bắt nạt", "người hùng nhí"],
    "id": ["bocah selamatkan", "anak selamatkan", "balita selamat", "siswa ditangkap", "siswa dikeluarkan karena",
           "penyakit langka anak", "anak ciptakan", "siswa dibully", "bocah ditemukan selamat", "pahlawan cilik"],
    "zh-Hans": ["男孩救", "女孩救", "孩子幸存", "学生被捕", "罕见病 孩子", "男孩发明", "学生被霸凌", "小英雄"],
    "zh-Hant": ["男童救", "女童救", "孩童生還", "學生被捕", "罕見疾病 兒童", "男童發明", "學生霸凌", "小英雄"],
    "ja": ["少年 救助", "少女 救助", "子ども 生還", "生徒 逮捕", "難病 子ども", "小学生 発明", "いじめ 生徒",
           "小さな英雄", "男児 無事発見"],
    "ko": ["소년 구조", "소녀 구했다", "아이 생존", "학생 체포", "희귀병 아이", "초등학생 발명", "학교폭력 학생", "꼬마 영웅"],
}

# Every country code: Google Trends "trending now" is read for all of them (countries it doesn't cover are skipped).
ALL_GEOS = """
AD AE AF AG AL AM AO AR AT AU AZ BA BB BD BE BF BG BH BI BJ BN BO BR BS BT BW BY BZ CA CD CF CG CH CI CL CM CN CO
CR CU CV CY CZ DE DJ DK DM DO DZ EC EE EG ER ES ET FI FJ FM FR GA GB GD GE GH GM GN GQ GR GT GW GY HK HN HR HT HU
ID IE IL IN IQ IR IS IT JM JO JP KE KG KH KI KM KN KP KR KW KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MG MH
MK ML MM MN MR MT MU MV MW MX MY MZ NA NE NG NI NL NO NP NR NZ OM PA PE PG PH PK PL PR PS PT PW PY QA RO RS RU RW
SA SB SC SD SE SG SI SK SL SM SN SO SR SS ST SV SY SZ TD TG TH TJ TL TM TN TO TR TT TV TW TZ UA UG US UY UZ VA VC
VE VN VU WS XK YE ZA ZM ZW
""".split()

ENGLISH_GEOS = set("""US GB CA AU IE NZ IN ZA PH NG SG KE MY PK GH UG TZ ZW ZM BW NA JM TT BS BB MT ET RW MW LR SL
GM FJ PG BZ GY LC GD VC AG DM KN SB VU WS TO KI TV NR PW MH FM SZ LS MU SC SS""".split())


def tag(loc: str, text: str) -> str:
    return f"[{loc}] {text}" if loc else text


def split_loc(query: str) -> tuple[str, str]:
    """'[pt-BR] menino salvou' -> ('pt-BR', 'menino salvou'); plain English -> ('', query)."""
    m = re.match(r"^\[([A-Za-z]{2}(?:-[A-Za-z]{2,4})?)\] (.+)$", query or "")
    return (m.group(1), m.group(2)) if m and m.group(1) in LOCALES else ("", query)


def lang_of(loc: str) -> str:
    """Language code for YouTube and autocomplete, e.g. 'pt' or 'zh-Hans'."""
    if loc.startswith("zh-"):
        return "zh-Hans" if loc == "zh-CN" else "zh-Hant"
    return loc.split("-")[0] if loc else "en"


def locale_for_geo(geo: str) -> str:
    return next((loc for loc, (_, gl, _) in LOCALES.items() if gl == geo), "")


def world_queries(cfg: dict) -> list[tuple[str, str]]:
    """Every translated search, tagged with each country edition its language is searched in."""
    wcfg = cfg.get("world") or {}
    if not wcfg.get("enabled"):
        return []
    langs = wcfg.get("languages", "all")
    out = []
    table = {**WORLD_QUERIES}
    for lang, extra in (wcfg.get("extra_queries") or {}).items():
        table[lang] = list(table.get(lang, [])) + list(extra or [])
    for lang, phrases in table.items():
        if langs != "all" and lang not in (langs or []):
            continue
        for loc in LANG_LOCALES.get(lang, [next((loc for loc in LOCALES if lang_of(loc) == lang), "")]):
            if loc:
                out += [(tag(loc, p), "") for p in phrases]
    return out


# ---------------------------------------------------------------- kid words and story words in other languages

def _rx(words_b: list[str], words_s: list[str]):
    """Words in languages with spaces need word edges; CJK, Thai, Korean, Arabic, Hebrew and Indic scripts are
    matched anywhere in the text."""
    b = re.compile(r"(?<!\w)(" + "|".join(words_b) + r")(?!\w)", re.I)
    s = re.compile("|".join(re.escape(w) for w in words_s))
    return b, s


KID_B, KID_S = _rx(
    [
        # es / pt / it / fr / ro / tl
        r"niñ[oa]s?", r"chic[oa]s?", r"menor(es)?", r"beb[éê]s?", r"adolescentes?", r"estudiantes?", r"alumn[oa]s?",
        r"hij[oa]s?", r"menin[oa]s?", r"crianças?", r"garot[oa]s?", r"alun[oa]s?", r"estudantes?", r"filh[oa]s?",
        r"bambin[oaie]", r"ragazz(o|a|i|e|ino|ina)", r"neonat[oa]", r"student(e|i|essa)", r"alunn[oaie]",
        r"enfants?", r"garçons?", r"fillettes?", r"élèves?", r"écoli(er|ère)s?", r"copi(l|ii|lul)", r"băiat\w*",
        r"fetiț\w*", r"elev(ă|i|ul)?", r"bata", r"batang",
        r"de \d{1,2} (años|anos|anni)", r"\d{1,2} ans",
        # de / nl / nordic / fi / hu
        r"kinder(n)?", r"jungen?", r"mädchen", r"schüler(in|innen)?", r"jugendliche[rn]?", r"\d{1,2}-jährige[rn]?",
        r"jongen", r"meisje", r"kinderen", r"leerlingen?", r"tieners?", r"barnet", r"pojke(n)?", r"flicka(n)?",
        r"gutt(en)?", r"jente(n)?", r"elev(en)?", r"lapsi", r"lapset", r"poika", r"tyttö", r"oppilas", r"gyere(k|kek)",
        r"gyermek", r"fiú", r"kislány", r"diák", r"tanuló",
        # pl / cs / sk / sl / sr / hr / lt / lv
        r"dzieck\w*", r"dzieci\w*", r"chłopiec", r"chłopc\w*", r"chłopak\w*", r"dziewczyn\w*", r"nastolat\w*",
        r"uczeń", r"uczni\w*", r"uczennic\w*", r"niemowl\w*", r"\d{1,2}-lat(ek|ka)", r"dítě", r"děti", r"chlap(ec|ce)",
        r"dívk\w*", r"holčičk\w*", r"žák\w*", r"školák\w*", r"dieťa", r"deti", r"dievč\w*", r"dečak\w*",
        r"devojčic\w*", r"dete", r"deca", r"otrok", r"deček", r"deklic\w*", r"vaik\w*", r"berniuk\w*", r"mergait\w*",
        r"mokin\w*", r"moksleiv\w*", r"paaugl\w*", r"kūdik\w*", r"bērn\w*", r"zēn\w*", r"meiten\w*",
        # ru / uk / bg / el
        r"ребен\w*", r"ребён\w*", r"дет(и|ей|ям|ьми|ях)", r"мальчик\w*", r"девочк\w*", r"школьн\w*", r"подрост\w*",
        r"младен\w*", r"малыш\w*", r"\d{1,2}-летн\w*", r"дитин\w*", r"діт(и|ей)", r"хлопчик\w*", r"дівчинк\w*",
        r"школяр\w*", r"підлітк\w*", r"немовл\w*", r"дете(то)?", r"момче(то)?", r"момиче(то)?", r"παιδ\w*",
        r"αγόρι\w*", r"κορίτσι\w*", r"μαθητ\w*", r"έφηβ\w*", r"μωρ\w*",
        # tr / id / ms / vi / sw
        r"çocu\w*", r"bebe\w*", r"öğrenci\w*", r"\d{1,2} yaşındaki", r"anak", r"bocah", r"balita", r"bayi", r"siswa",
        r"siswi", r"pelajar", r"remaja", r"murid", r"em bé", r"bé trai", r"bé gái", r"cậu bé", r"cô bé", r"cháu bé",
        r"học sinh", r"trẻ em", r"thiếu niên", r"mtoto", r"watoto", r"mvulana", r"msichana", r"mwanafunzi",
    ],
    [
        # ar / fa / ur / he
        "طفل", "طفلة", "أطفال", "صبي", "فتاة", "طالب", "طالبة", "رضيع", "مراهق", "کودک", "بچه", "دختر", "پسر",
        "بچے", "لڑکا", "لڑکی", "ילד", "ילדה", "ילדים", "נער", "נערה", "תלמיד", "תינוק",
        # hi / mr / bn / ta / te / ml
        "बच्चा", "बच्चे", "बच्ची", "लड़का", "लड़की", "छात्र", "छात्रा", "किशोर", "मासूम", "मुलगा", "मुलगी", "मूल",
        "শিশু", "ছেলে", "মেয়ে", "ছাত্র", "ছাত্রী", "কিশোর", "சிறுவன்", "சிறுமி", "குழந்தை", "மாணவ", "బాలుడు",
        "బాలిక", "చిన్నారి", "విద్యార్థి", "കുട്ടി", "ബാലൻ", "ബാലിക", "വിദ്യാർത്ഥി",
        # th / zh / ja / ko
        "เด็ก", "นักเรียน", "ทารก", "男孩", "女孩", "孩子", "儿童", "兒童", "男童", "女童", "孩童", "小学生", "中学生",
        "学生", "學生", "婴儿", "嬰兒", "少年", "少女", "男児", "女児", "子ども", "子供", "児童", "生徒",
        "高校生", "赤ちゃん", "乳児", "아이", "어린이", "소년", "소녀", "학생", "초등학생", "중학생", "아기", "영아",
    ],
)

STORY_B, STORY_S = _rx(
    [
        # es / pt
        r"salv[óoa]u?", r"rescat\w*", r"resgat\w*", r"h[ée]ro(e|i|ína)s?", r"heroína", r"sobreviv\w*", r"viral",
        r"arrestad[oa]", r"detenid[oa]", r"pres[oa]", r"suspens[oa]", r"suspendid[oa]", r"expulsad[oa]", r"expuls[oa]",
        r"acoso", r"bullying", r"rar[oa]", r"médicos", r"invent(ó|ou)", r"descobriu", r"descubrió", r"increíble",
        r"insólito", r"inacreditável", r"conmovedor", r"comovente", r"emocionante", r"karma",
        # fr / it / ro
        r"sauvé", r"sauve", r"héros", r"héroïne", r"survécu", r"arrêté", r"suspendu", r"exclu", r"harcèlement",
        r"harcelé", r"rare", r"médecins", r"inventé", r"découvert", r"incroyable", r"émouvant", r"salvat[oa]",
        r"salva", r"eroe", r"eroina", r"sopravvissut[oa]", r"virale", r"arrestat[oa]", r"sospes[oa]", r"bullismo",
        r"medici", r"inventat[oa]", r"scopert[oa]", r"incredibile", r"commovente", r"a salvat", r"erou",
        r"supraviețuit", r"arestat", r"agresat", r"inventat",
        # de / nl / nordic / fi / hu
        r"rettet", r"gerettet", r"held(in)?", r"überlebt", r"festgenommen", r"verhaftet", r"suspendiert", r"mobbing",
        r"gemobbt", r"seltene", r"ärzte", r"erfindet", r"erfunden", r"entdeckt", r"unglaublich", r"rührend", r"redt",
        r"gered", r"overleeft", r"overleefd", r"viraal", r"gearresteerd", r"geschorst", r"gepest", r"zeldzame",
        r"artsen", r"ontdekt", r"räddade", r"reddet", r"överlevde", r"overlevde", r"hjälte", r"helt", r"mobbad",
        r"mobbet", r"pelasti", r"sankari", r"megmentette", r"hős", r"túlélte", r"ritka",
        # pl / cs / sk / lt / lv / sl / sr
        r"uratował\w*", r"bohater\w*", r"przeżył\w*", r"zatrzyman\w*", r"aresztowan\w*", r"zawieszon\w*",
        r"prześladow\w*", r"rzadk\w*", r"lekarz\w*", r"wynalaz\w*", r"odkrył\w*", r"wzrusz\w*", r"zachránil\w*",
        r"hrdin\w*", r"přežil\w*", r"prežil\w*", r"vzácn\w*", r"šikan\w*", r"išgelbėj\w*", r"didvyr\w*", r"herojus",
        r"išgyven\w*", r"sulaikyt\w*", r"patyč\w*", r"gydytoj\w*", r"išrad\w*", r"izglāb\w*", r"varon\w*",
        r"izdzīvo\w*", r"rešil\w*", r"junak\w*", r"spas\w*", r"heroj\w*", r"preživel\w*",
        # ru / uk / bg / el
        r"спас\w*", r"геро\w*", r"выжил\w*", r"задерж\w*", r"арест\w*", r"травл\w*", r"редк\w*", r"врач\w*",
        r"изобр\w*", r"нашли", r"трогательн\w*", r"врятув\w*", r"вижил\w*", r"булінг", r"рідкісн\w*", r"винайш\w*",
        r"оцеля\w*", r"рядко", r"έσωσε", r"ήρωας", r"επέζησε", r"συνελήφθη", r"σπάνια",
        # tr / id / vi
        r"kurtard\w*", r"kahraman\w*", r"hayatta kald\w*", r"gözaltı\w*", r"tutuklan\w*", r"uzaklaştırma",
        r"zorbalı\w*", r"nadir", r"doktor\w*", r"icat", r"keşfet\w*", r"inanılmaz", r"selamatkan", r"menyelamatkan",
        r"pahlawan", r"selamat", r"ditangkap", r"diskors", r"dikeluarkan", r"dibully", r"perundungan", r"langka",
        r"dokter", r"ciptakan", r"menemukan", r"cứu", r"anh hùng", r"người hùng", r"sống sót", r"bị bắt",
        r"bắt nạt", r"hiếm gặp", r"bác sĩ", r"chế tạo", r"phát minh", r"phát hiện", r"đình chỉ",
    ],
    [
        "أنقذ", "ينقذ", "تنقذ", "بطل", "نجا", "نجاة", "اعتقال", "تنمر", "نادر", "الأطباء", "اخترع", "يخترع", "اكتشف",
        "הציל", "גיבור", "שרד", "נעצר", "בריונות", "נדירה", "המציא", "बचाया", "बचाई", "बचा", "हीरो", "बहादुर",
        "जिंदा", "गिरफ्तार", "दुर्लभ", "डॉक्टर", "बनाया", "बुलिंग", "वायरल", "वाचवले", "दुर्मिळ", "বাঁচাল", "বেঁচে",
        "গ্রেপ্তার", "বিরল", "নায়ক", "காப்பாற்ற", "உயிர்", "அரிய", "కాపాడ", "ప్రాణాల", "అరుదైన", "രക്ഷി", "അപൂർവ",
        "ช่วยชีวิต", "รอดชีวิต", "ถูกจับ", "หายาก", "ประดิษฐ์", "บูลลี่", "ฮีโร่", "救", "英雄", "幸存", "生还", "生還",
        "被捕", "逮捕", "霸凌", "欺凌", "罕见", "罕見", "医生", "醫生", "发明", "發明", "发现", "發現", "走红", "感动",
        "感動", "救助", "生還", "いじめ", "珍しい", "難病", "医師", "発明", "発見", "話題", "無事", "구조", "구했",
        "영웅", "생존", "체포", "괴롭힘", "학교폭력", "희귀", "의사", "발명", "발견", "화제", "감동",
    ],
)

# sexual content in other languages never goes to the AI (the English list is in prefilter.py)
BLOCK_INTL = re.compile(
    "|".join(re.escape(w) for w in [
        "abuso sexual", "violación", "violada", "estupro", "agresión sexual", "agressão sexual", "viol ",
        "agression sexuelle", "pédophil", "vergewaltig", "sexuell", "missbrauch", "violenza sessuale", "stupro",
        "molestat", "gwałt", "molestow", "изнасил", "сексуал", "растлен", "згвалтув", "cinsel istismar", "cinsel taciz",
        "tecavüz", "pelecehan seksual", "pencabulan", "pemerkosaan", "cabul", "hiếp dâm", "xâm hại tình dục",
        "اغتصاب", "تحرش", "هتك عرض", "אונס", "הטרדה מינית", "बलात्कार", "यौन", "दुष्कर्म", "ধর্ষণ", "யௌன",
        "ข่มขืน", "อนาจาร", "性侵", "猥亵", "猥褻", "强奸", "強姦", "性的暴行", "性被害", "性犯罪", "わいせつ", "盗撮", "성폭행", "성추행", "성범죄",
        "seksualn", "znásiln", "seksuaal", "prievart",
    ]),
    re.I,
)


def intl_counts(text: str) -> tuple[int, int]:
    """(kid words, story words) found in other languages."""
    return (len(KID_B.findall(text)) + len(KID_S.findall(text)),
            len(STORY_B.findall(text)) + len(STORY_S.findall(text)))


def is_spaced(text: str) -> bool:
    return " " in text.strip()
