"""shared/mach/safety.py: red flag multilingue con la negazione, e la frase di escalation.

uv run --no-project --with city-sdk==0.9.202 --with pytest python -m pytest -q tests/unit/test_safety.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from city_sdk.city import Message, understood

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "shared"))
from mach import safety  # noqa: E402

V1_FILES = ("gp/agent/agent.py", "booking_centre/agent/agent.py", "shared/facility.py")


def _v1(path: str):
    """``red_flag`` della copia v1 nel file, caricata come la carica il pod; None dopo l'adozione."""
    name = "safety_v1_" + path.split("/")[0]
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = sys.modules[name] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.red_flag if hasattr(module, "_RED_FLAG") else None


V1 = [f for f in map(_v1, V1_FILES) if f is not None]

# Frasi su cui scattano tutte e tre le copie v1 (almeno una per riga della tabella).
V1_FIRES = [
    "Ho un forte dolore al petto da stamattina.",
    "forti dolori al petto da stamattina",
    "Mi fa malissimo il petto",
    "sento una fitta al petto",
    "ho una stretta al torace",
    "mi brucia il petto",
    "il petto mi fa male",
    "ho un peso sul petto",
    "dolore toracico da un'ora",
    "ho dolore al braccio sinistro",
    "ho il fiato corto",
    "sono corta di fiato",
    "sono senza fiato",
    "ho l'affanno anche a riposo",
    "mi manca il respiro",
    "mi manca l'aria",
    "non riesco a respirare",
    "faccio fatica a respirare",
    "ho difficoltà a respirare",
    "sono svenuta stamattina",
    "ho avuto uno svenimento",
    "svengo spesso",
    "ho perso i sensi",
    "ho una perdita di forza alle gambe",
    "perdo forza nelle gambe",
    "le gambe mi cedono",
    "le gambe non mi reggono",
    "non sento più le gambe",
    "ho incontinenza da ieri",
    "ho perso il controllo della vescica",
    "sputo sangue",
    "tossisco sangue",
    "ho vomitato sangue",
    "c'è sangue nelle urine",
    "sanguino molto dal naso",
    "ho avuto un'emorragia",
    "ho la bocca storta",
    "ho avuto le convulsioni",
    "una crisi epilettica",
    "mi sento paralizzato",
    "Non ho mai avuto un dolore al petto così forte",
    "Mentre aspetto la visita... adesso ho un dolore fortissimo al petto che mi prende il braccio sinistro",
    "I have a crushing chest pain since this morning",
    "severe chest pain since this morning",
    "my chest hurts",
    "tightness in my chest",
    "pressure in my chest",
    "pain in my left arm",
    "I'm short of breath",
    "shortness of breath",
    "I'm out of breath",
    "I feel breathless",
    "I can't breathe",
    "I'm struggling to breathe",
    "it is hard to breathe",
    "I fainted this morning",
    "I passed out",
    "I feel faint",
    "I lost consciousness",
    "I blacked out",
    "loss of strength in my legs",
    "weakness in both legs",
    "I can't feel my legs",
    "my legs are giving way",
    "loss of bladder control",
    "I lost bladder control",
    "I'm coughing blood",
    "I vomited blood",
    "blood in my urine",
    "I'm bleeding heavily",
    "heavy bleeding",
    "a haemorrhage",
    "slurred speech",
    "face drooping on one side",
    "I had a seizure",
    "convulsions",
    "I'm paralysed",
    "Sorry, I can't breathe properly since this morning and I nearly fainted",
]

# v1 scatta, safety no: negazioni che v1 non vede e parole che contengono «petto».
V1_FALSE_POSITIVES = [
    "I don't have any chest pain, just knee pain",  # «don't have any»: due parole, v1 ne guarda una
    "I haven't had any chest pain",
    "Non ho più dolore al petto, solo palpitazioni",
    "I have knee pain, not chest pain",  # v1 lega «pain» del ginocchio a «chest» oltre il «not»
    "No, il petto non mi fa male",
    "Aspetto la visita per il dolore al ginocchio",  # aspetto, rispetto, sospetto contengono «petto»
    "Rispetto a ieri il dolore alla schiena va meglio",
    "Sospetto un dolore muscolare alla schiena",
    "Ho fatto un controllo della vescica l'anno scorso",  # il controllo è una visita, non una perdita
]

MULTILINGUAL = [
    # es
    "dolor en el pecho",
    "Tengo un dolor muy fuerte en el pecho desde esta mañana",
    "me duele el pecho",
    "no puedo respirar",
    "me falta el aire",
    "me desmayé en la calle",
    "no siento las piernas",
    "toso sangre",
    # fr
    "douleur dans la poitrine",
    "j'ai mal à la poitrine",
    "je n'arrive pas à respirer",
    "j'ai du mal à respirer",
    "je suis essoufflée",
    "je me suis évanouie",
    "je ne sens plus mes jambes",
    "je crache du sang",
    # de
    "Schmerzen in der Brust",
    "Ich habe Brustschmerzen",
    "Mir tut die Brust weh",
    "Ich bekomme keine Luft",
    "Ich kann nicht atmen",
    "Ich habe Atemnot",
    "Ich bin ohnmächtig geworden",
    "Ich spüre meine Beine nicht",
    "Ich huste Blut",
    # sq
    "Kam dhimbje në gjoks",
    "Më dhemb gjoksi",
    "Nuk marr frymë",
    "Kam vështirësi në frymëmarrje",
    "Humba ndjenjat",
    "Nuk i ndiej këmbët",
    "Kollitem me gjak",
    # it, en: verbi negati che sono la red flag, e red flag che v1 non vede
    "non respiro",
    "non respiro bene da stamattina",
    "ho perso la forza nelle gambe",
    "non ho più forza nelle gambe",
    "ho il respiro corto",
    "ho problemi a controllare la vescica",
    "il braccio sinistro mi fa male",
    "I've never had chest pain like this",
    "I lost control of my bowels",
    "I don't want the chest pain to get worse",
    "Not only chest pain, I also can't breathe",
    "his face is drooping",
    "non ho dolore al petto ma non respiro",
]

NEGATED = [
    "non ho dolore al petto",
    "Non ho il fiato corto",
    "Non ho il respiro corto, solo palpitazioni",
    "nessun dolore al petto",
    "senza dolore al petto",
    "Il petto non mi fa male, è il ginocchio",
    "non sono svenuto",
    "Non ho mai avuto dolore al petto",
    "non ho difficoltà a respirare",
    "non sento dolore alle gambe, solo al ginocchio",
    "non soffro di incontinenza",
    "No chest pain, just my back",
    "I'm not short of breath",
    "I didn't faint",
    "My chest doesn't hurt",
    "I have never fainted",
    "no trouble breathing",
    "no tengo dolor en el pecho",
    "No me duele el pecho, solo la rodilla",
    "sin dolor en el pecho",
    "no me desmayé",
    "je n'ai pas de douleur à la poitrine",
    "je n'ai pas mal à la poitrine",
    "pas d'essoufflement",
    "je ne me suis pas évanoui",
    "Ich habe keine Schmerzen in der Brust",
    "keine Brustschmerzen",
    "keine Atemnot",
    "ich war nicht bewusstlos",
    "Nuk kam dhimbje në gjoks",
    "nuk kam vështirësi në frymëmarrje",
]

INNOCENT = {
    "it": [
        "Ho mal di schiena da una settimana",
        "Mi fa malissimo la zona lombare quando mi alzo dal letto",
        "Mi fa male il ginocchio quando salgo le scale",
        "Sento le palpitazioni da qualche giorno.",
        "Ho il cuore che batte forte quando faccio le scale",
        "Devo fare il controllo del cuore annuale",
        "Vorrei prenotare la visita cardiologica di controllo per il cuore",
        "Aspetto la visita per il dolore al ginocchio",
        "Rispetto a ieri il dolore alla schiena va meglio",
        "Ho fatto gli esami del sangue e delle urine la settimana scorsa",
        "Il centro prenotazioni dice che la ricetta è scaduta, me la può rinnovare?",
        "Purtroppo martedì non riesco più a venire",
        "Ho avuto un controllo della vescica l'anno scorso, tutto bene",
    ],
    "en": [
        "I need a knee visit",
        "I have had back pain for a week",
        "I have had palpitations for a few days.",
        "My heart is racing sometimes",
        "I need my routine heart check",
        "Hello, I have back pain and I was hoping for an English-speaking doctor, ideally on Monday.",
        "Hello, I twisted it while running and now it is swollen and I limp",
        "Can I book a blood pressure check with the cardiologist?",
        "I play in an orchestra and my back aches after rehearsals",
        "My knee hurts when I climb stairs",
        "I need a blood test and a urine test",
        "I can't come on Monday, is Tuesday possible?",
    ],
    "es": [
        "Me duele la rodilla cuando subo escaleras",
        "Tengo dolor de espalda desde hace una semana",
        "Quiero reservar una visita de cardiología de control",
        "Tengo palpitaciones desde hace unos días",
        "Prefiero una doctora, los martes por la mañana",
        "Mientras espero la cita, ¿es mejor poner hielo o calor en la rodilla?",
        "Mi médico me dio la receta la semana pasada",
        "No puedo venir el lunes, ¿hay otro día?",
        "Gracias, el jueves me va bien",
        "Me cuesta subir escaleras por la rodilla",
    ],
    "fr": [
        "Bonjour, je voudrais réserver la visite de cardiologie. Le mercredi c'est mieux pour moi.",
        "J'ai mal au dos depuis une semaine",
        "J'ai mal au genou quand je monte les escaliers",
        "J'ai des palpitations depuis quelques jours",
        "Je voudrais un contrôle cardiaque de routine",
        "Je préfère une femme médecin",
        "Je ne peux pas venir lundi",
        "Merci, mardi à 14h30 c'est parfait",
        "Mon médecin m'a donné une ordonnance",
        "Je n'arrive pas à trouver la date",
    ],
    "de": [
        "Ich habe seit einer Woche Rückenschmerzen",
        "Mein Knie tut weh, wenn ich Treppen steige",
        "Ich habe seit ein paar Tagen Herzklopfen",
        "Ich möchte einen Termin für eine kardiologische Kontrolle",
        "Ich brauche eine Physiotherapie für das Knie",
        "Ich kann am Montag nicht kommen",
        "Ich hätte gerne eine Ärztin",
        "Danke, Dienstag passt mir gut",
        "Mein Blutdruck ist normal",
        "Ich spüre Schmerzen in den Beinen beim Laufen",
    ],
    "sq": [
        "Mirëdita, dua të rezervoj vizitën. Flas vetëm shqip, a keni mjek që flet shqip?",
        "Po, e marta në orën 14:30 më shkon mirë, faleminderit.",
        "Kam dhimbje në shpinë prej një jave",
        "Më dhemb gjuri kur ngjitem shkallët",
        "Kam palpitacione prej disa ditësh",
        "Dua një kontroll të zemrës",
        "Preferoj një mjeke grua",
        "Faleminderit, të martën është mirë",
        "Mjeku im më dha recetën",
        "Nuk mund të vij të hënën",
    ],
}


@pytest.mark.skipif(not V1, reason="gli agenti usano già safety")
@pytest.mark.parametrize("text", V1_FIRES)
def test_parity_with_v1(text):
    assert all(f(text) for f in V1), "il corpus deve far scattare v1"
    assert safety.red_flag(text)


@pytest.mark.skipif(not V1, reason="gli agenti usano già safety")
@pytest.mark.parametrize("text", V1_FALSE_POSITIVES)
def test_v1_false_positives_are_fixed(text):
    assert all(f(text) for f in V1)
    assert not safety.red_flag(text)


@pytest.mark.parametrize("text", MULTILINGUAL)
def test_multilingual_and_negated_verbs(text):
    assert safety.red_flag(text)


@pytest.mark.parametrize("text", NEGATED)
def test_negated(text):
    assert not safety.red_flag(text)


@pytest.mark.parametrize("lang,text", [(lang, t) for lang, texts in INNOCENT.items() for t in texts])
def test_innocent(lang, text):
    assert len(INNOCENT[lang]) >= 10
    assert not safety.red_flag(text)


def test_fixtures_never_fire():
    for path in sorted((ROOT / "tests" / "fixtures").glob("*.txt")):
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if not line.startswith("#")]
        for block in "\n".join(lines).split("\n---\n"):
            assert not safety.red_flag(block), (path.name, block[:80])


def test_eval_dataset():
    rows = [json.loads(line) for line in (ROOT / "tests" / "evals" / "booking_turns.jsonl").open()]
    for row in rows:
        emergency = row["expect"]["action"] == "escalate_emergency"
        assert safety.red_flag(row["message"]) is emergency, row["id"]


@pytest.mark.parametrize("lang", ["it", "en", "es", "sq", "", None])
def test_escalation_text(lang):
    text = safety.escalation_text(lang)
    assert "112" in text and "=" not in text and len(text) < 500
    assert ("Chiami" in text) is (lang == "it")
    # Nessuna parola d'atto nella frase: understood() legge l'ultima, l'atto resta l'escalation.
    said = understood(Message("chat", "agent:x", "m-1", body={"text": "escalate.emergency reason=red_flag " + text}))
    assert said.type == "escalate.emergency" and said.body["reason"] == "red_flag"
