"""Red flag nelle parole del cittadino e frase di escalation: una sola tabella per ogni agente.

Il protocollo elenca le red flag come codici (RF-...), che nessun cittadino scrive: si leggono le
parole, in it, en, es, fr, de e sq, su testo minuscolo e senza accenti. Unione delle tre copie
di v1 (gp, booking, strutture) e della tabella di ``v2/core.py``, con i confini di parola.

Negazione: «non ho dolore al petto» non conta, ma solo se tra la negazione e la red flag ci sono
al più tre parole di servizio (ausiliari, articoli, «più», «any»...): «I don't want the chest
pain to...» conta. Un verbo negato che è esso stesso la red flag («non respiro», «I can't
breathe», «ich bekomme keine Luft») ha la negazione dentro il pattern, quindi conta. «Mai»
seguito da «così»/«like this» conta. Le palpitazioni da sole sono un sintomo di protocollo
(SYM-PALPITATIONS → VIS-CARD), non un'emergenza.

Il chiamante scrive l'atto: ``"escalate.emergency reason=red_flag " + escalation_text(lang)``.
Solo stdlib.
"""

from __future__ import annotations

import re
import unicodedata

# Negazioni. «can't», «non riesco», «nuk marr» non ci sono: dentro una red flag sono la red flag.
_NEG = (
    r"non|no|nessun\w*|senza|niente|nulla|mai|neanche|nemmeno|not|never|without|none|nor"
    r"|don'?t|doesn'?t|didn'?t|haven'?t|hasn'?t|hadn'?t|isn'?t|aren'?t|wasn'?t|weren'?t"
    r"|sin|nunca|ningun\w*|nada|pas|aucun\w*|jamais|sans|nicht|kein\w*|nie|niemals|ohne|nuk|pa|kurre"
)
_NEVER = frozenset({"mai", "never", "nunca", "jamais", "nie", "niemals", "kurre"})
# Parole di servizio ammesse tra la negazione e la red flag: ogni altra parola la interrompe.
_GAP = (
    r"ho|hai|ha|abbiamo|avuto|avevo|sono|stato|stata|sento|sentito|soffro|provo|avverto|fa|fanno"
    r"|mi|ti|si|ci|c|e|piu|alcun\w*|ancora|un|una|uno|il|lo|la|i|gli|le|l|di|d|del|della|dei|delle"
    r"|al|alla|have|has|had|having|feel|felt|feeling|get|got|getting|experienc\w*|suffer\w*"
    r"|am|is|are|was|were|be|been|any|more|anymore|longer|a|an|the|my|some"
    r"|tengo|tiene|tenido|tuve|he|siento|sentido|sufro|estoy|me|te|se|el|los|las|mas|de"
    r"|ai|as|eu|sens|ressens|ressenti|suis|plus|du|des|les|j|m|n"
    r"|habe|hab|hatte|bin|war|spure|mehr|ich|mir|die|der|das|den|einen|eine"
    r"|kam|kisha|kemi|pata|ndiej|ndjej|ndjeva|jam|asnje|ndonje"
)
_NEGATED = re.compile(rf"\b({_NEG})\s+(?:(?:{_GAP})\s+){{0,3}}$")
_LIKE_THIS = re.compile(
    r"[^.!?;]{0,30}?\b(?:cosi|like (?:this|that)|before|prima d ora|so (?:bad|strong)|como (?:este|esto|ahora)"
    r"|tan fuerte|comme (?:ca|celle)|si fort\w*|wie (?:jetzt|diese\w*)|so stark)\b"
)

_PAIN = (
    r"(?:dolor\w*|mal(?:e|issimo)?\b|fitt\w*|strett\w*|string\w*|oppress\w*|bruci\w*|pain\w*|hurt\w*"
    r"|ach\w*|tight\w*|pressure|squeez\w*|duel\w*|douleur\w*|schmerz\w*|weh\b|druck\w*|dhimbj\w*|dhemb\w*)"
)
_CHEST = r"(?:petto|th?ora[cx]\w*|chest|pecho|poitrine|brust\w*|gjoks\w*)"
_LEFT_ARM = r"(?:braccio sinistro|left arm|brazo izquierdo|bras gauche|linke\w* arm|krah\w* (?:e |i )?majte)"
_LEGS = r"(?:gamb\w*|legs?\b|piern\w*|jambes?\b|beine?n?\b|kemb\w*)"
# Fino a quattro parole tra dolore e petto, nessuna una negazione: «knee pain, not chest pain».
_NEAR = rf"\W+(?:(?!(?:{_NEG})\b)\w+\W+){{0,4}}?"
_UPTO2 = r"\W+(?:\w+\W+){0,2}?"
_UPTO3 = r"\W+(?:\w+\W+){0,3}?"
_RED_FLAG = re.compile(r"\b(?:" + "|".join((
    # dolore al petto, al braccio sinistro
    _PAIN + _NEAR + _CHEST,
    _CHEST + _NEAR + _PAIN,
    r"peso (?:sul|al) petto|brustschmerz\w*|brustenge|engegefuhl",
    _PAIN + _NEAR + _LEFT_ARM,
    _LEFT_ARM + _NEAR + _PAIN,
    # respiro
    r"fiato corto|respiro corto|cort\w* di fiato|senza fiato|affann\w*|dispne\w*|disnea|short(?:ness)? of breath"
    r"|out of breath|breathless\w*|essouffl\w*|atemnot|luftnot|kurzatmig\w*|ahog\w*|me merr fryma",
    r"(?:manc\w*|falt\w*)" + _UPTO2 + r"(?:fiato|respiro|aria|aire)\b",
    r"(?:non riesco|fatic\w*|dif+ic\w*|can'?t|cannot|unable to|hard to|trouble|struggl\w*|no puedo|cuesta"
    r"|n arrive|ne peux|ne peut|du mal|kann (?:nicht|kaum)|schwer|nuk (?:mund te )?marr|s marr|veshtires\w*)"
    + _UPTO2 + r"(?:respir\w*|breath\w*|atme\w*|atmen|frym\w*)",
    r"non (?:piu )?respir(?:o|a|ano)\b|(?:bekomm\w*|krieg\w*) (?:kaum |keine )luft",
    # svenimento
    r"svenu\w*|svenim\w*|sven(?:go|gono|ire)\b|faint(?:ed|ing)\b|(?:feel\w*|felt) faint|pass(?:ed|ing)? out"
    r"|lost consciousness|black(?:ed)? out|desmay\w*|evanoui\w*|ohnmacht\w*|bewusstlos\w*|umgekippt"
    r"|humb\w* (?:ndjenjat|vetedijen)|te fiket|perte de connaissance",
    r"(?:pers\w*|perd\w*)\W+(?:(?:i|il|el|la) )?(?:sensi|conoscenza|coscienza|conocimiento|conciencia|connaissance)",
    # gambe; «non sento dolore alle gambe» non è perdita di sensibilità
    r"(?:perdita di forza|per[sd]\w* (?:la )?forza|non ho (?:piu )?forza|debolezza|loss of strength"
    r"|losing strength|lost (?:my )?strength|no strength|weak\w*|perd\w* (?:la )?fuerza|sin fuerza|debilidad"
    r"|perte de force|faiblesse|keine kraft|kraftlos\w*|dobesi)" + _UPTO3 + _LEGS,
    r"(?:non (?:mi )?sento|can'?t feel|cannot feel|no siento|ne sens|nuk (?:i )?ndiej)"
    + rf"\W+(?:(?!{_PAIN})\w+\W+){{0,3}}?" + _LEGS,
    r"(?:spure|fuhle) (?:\w+ )?beine?n? (?:nicht|kaum)",
    _LEGS + _UPTO2 + r"(?:cedono|non (?:mi )?(?:reggono|rispondono)|paralizzat\w*|giving (?:out|way)"
    r"|gave (?:out|way)|won'?t hold|weak\w*|no me (?:sostienen|responden)|fallan|ne me portent|lachent"
    r"|schwach\w*|gelahmt|nachgeben|nuk me mbajne)",
    # vescica e intestino
    r"in[ck]ontinen\w*",
    r"(?:loss of|lost|can'?t control)" + _UPTO2 + r"(?:bladder|bowel)",
    # «perdo il controllo della vescica»; il solo «controllo della vescica» è una visita
    r"(?:perd\w*|pers\w*|perte|lost|losing|loss|verlier\w*|verlor\w*|humb\w*|non|no|nuk|keine?|can'?t|cannot"
    r"|problem\w*|dif+ic\w*|fatic\w*|trouble)" + _UPTO2 + r"(?:controll?\w*|kontroll\w*)" + _UPTO3
    + r"(?:vescica|intestin\w*|sfinter\w*|bladder|bowel\w*|vejiga|esfinter\w*|vessie|blase|darm\w*|fshikez\w*|zorr\w*)",
    # sangue
    r"(?:sput\w*|tossisc\w*|tos\w*|vomi\w*|perd\w*|cough\w*|spit\w*|escup\w*|crach\w*|touss\w*|hust\w*"
    r"|erbrech\w*|spuck\w*|kollit\w*|vjell\w*|peshty\w*)" + _UPTO2 + r"(?:sangue|blood|sangre|sang|blut|gjak\w*)\b",
    # «sangue nelle urine», non «esami del sangue e delle urine»
    r"(?:sangue|blood|sangre|sang|blut|gjak)\W+(?:\w+\W+)?(?:nelle|nel|dalla|in|en|dans|im|ne)\W+(?:\w+\W+)?"
    r"(?:urin\w*|feci|stool|vomit\w*|orina|heces|selles|stuhl|jashteqitj\w*|bocca)",
    r"bluthust\w*|bluterbrech\w*",
    r"sanguin\w*\W+(?:\w+\W+)?(?:molto|tanto|abbondant\w*|non si ferma)|bleed\w*\W+(?:\w+\W+)?(?:heavily|a lot|won'?t stop)"
    r"|heavy bleeding|h?a?emorr?h?ag\w*|sangr\w*\W+(?:\w+\W+)?(?:mucho|abundant\w*|no para)"
    r"|saign\w*\W+(?:\w+\W+)?(?:beaucoup|abondamment)|stark\w* blutung\w*|gjakderdhj\w*",
    # ictus e convulsioni
    r"paral[iy]\w*|(?:faccia|bocca) storta|slurred speech|face (?:is )?droop\w*|cara (?:torcida|caida)|bouche de travers"
    r"|lahmung\w*|gelahmt\w*",
    r"[ck]onvulsi\w*|seizure\w*|crisi epilettic\w*|crise d epilep\w*|krampfanfall\w*|epilepti\w* anfall\w*",
)) + ")")


def plain(text: str) -> str:
    """Minuscole senza accenti; l'elisione diventa uno spazio («d'aria» → «d aria»)."""
    folded = unicodedata.normalize("NFKD", (text or "").replace("’", "'")).casefold()
    flat = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return re.sub(r"\b(\w)'", r"\1 ", flat)


def red_flag(text: str) -> bool:
    """True se il cittadino descrive ORA una red flag, in qualsiasi punto del testo."""
    flat = plain(text)
    for hit in _RED_FLAG.finditer(flat):
        neg = _NEGATED.search(flat, max(0, hit.start() - 40), hit.start())
        if neg is None or (neg.group(1) in _NEVER and _LIKE_THIS.match(flat, hit.end())):
            return True
    return False


_ESCALATION = {
    "it": (
        "Mi dispiace, dev'essere un momento difficile. Chiami subito il 112 o vada al pronto soccorso "
        "più vicino. Se c'è qualcuno con lei, gli chieda di chiamare per lei e di restarle accanto. "
        "Passata l'urgenza, mi riscriva e la aiuto con i prossimi passi."
    ),
    "en": (
        "I'm sorry, this must be a difficult moment. Call 112 now or go to the nearest emergency "
        "department. If someone is with you, ask them to make the call for you and stay with you. "
        "Once the emergency is over, write to me again and I will help you with the next steps."
    ),
}


def escalation_text(lang: str | None) -> str:
    """La frase al cittadino, senza token né parole d'atto: in italiano per ``it``, se no in inglese."""
    return _ESCALATION["it" if lang == "it" else "en"]
