"""Conservative, local interpretation. Unknown/ambiguous language is not a fact.

This is a small closed ontology, not open-ended extraction or an LLM classifier.
Pronouns are interpreted from the USER's perspective only. Confidence measures
explicit attribution, never trading performance or independent verification.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_TEXT = 1000
RELATIONS = frozenset({
    "developed_by", "called", "alias_of", "default_timeframe", "default_symbol",
    "prefers", "uses", "works_on",
})
SYMBOLS = {"gold": "XAUUSD", "silver": "XAGUSD", "euro dollar": "EURUSD",
           "pound dollar": "GBPUSD", "bitcoin": "BTCUSD"}
PREFIX = re.compile(
    r"^(?:(?:hey\s+)?tars[, :]+)?(?:please\s+)?"
    r"(?:(?:remember(?:\s+that)?|keep\s+in\s+mind(?:\s+that)?|from\s+now\s+on)\s*[, :]+)",
    re.I,
)
CORRECTION = re.compile(r"^(?:actually|correction|instead|update(?:\s+that)?)[, :]+", re.I)
SECRET = re.compile(
    r"\b(?:password|passwd|passphrase|api[ _-]?key|access[ _-]?token|auth[ _-]?token|"
    r"secret[ _-]?key|private[ _-]?key|recovery[ _-]?(?:phrase|code)|seed phrase|"
    r"credential|bearer)\b|\b(?:sk-|ghp_|AIza)[A-Za-z0-9_-]{12,}|"
    r"-----BEGIN.*PRIVATE KEY|https?://[^\s/]+:[^\s/]+@", re.I,
)
TRANSIENT = re.compile(
    r"\b(?:right now|today|tonight|tomorrow|this (?:session|conversation|time)|"
    r"for now|temporarily|current price|price is|trading at|bid is|ask is|spread is)\b", re.I,
)
UNCERTAIN = re.compile(
    r"\b(?:maybe|perhaps|might|possibly|probably|suppose|imagine|if|apparently|"
    r"i (?:think|guess)|not|never|didn't|wasn't|isn't|don't|doesn't)\b|\?", re.I,
)
DEVELOPER = r"(?:built|created|developed|made)(?:\s+or\s+(?:built|created|developed|made))?"
ASSISTANT = r"(?:you|yourself|tars|this assistant)"
NAME = r"[\w]+(?:[ '-][\w]+){0,5}"


def clean(text: str) -> str:
    return " ".join(text.replace("’", "'").split()).strip(" .!\"“”")


def entity(text: str) -> str:
    value = clean(text)
    value = re.sub(r"^(?:a\s+)?developer\s+(?:(?:called|named)\s+)?", "", value, flags=re.I)
    if value.lower() in {"you", "yourself", "your", "tars", "this assistant"}:
        return "TARS"
    if value.lower() in {"i", "me", "my", "myself", "user", "the user"}:
        return "USER"
    return value


def symbol(text: str) -> str | None:
    value = clean(text)
    return SYMBOLS.get(value.lower()) or (
        value.upper() if re.fullmatch(r"[a-zA-Z]{6}|[A-Z]{2,5}(?:[._][A-Za-z]+)?", value) else None
    )


def timeframe(text: str) -> str | None:
    value = clean(text).lower()
    match = re.fullmatch(r"(\d{1,3})\s*(m|min|mins|minutes?|h|hours?|d|days?|w|weeks?)", value)
    if not match or int(match[1]) == 0:
        return None
    unit = match[2][0]
    amount = int(match[1])
    if unit == "h":
        amount, unit = amount * 60, "m"
    if unit == "m" and amount % 60 == 0:
        return f"{amount // 60}h"
    return f"{amount}{unit}"


def developer(text: str, user_name: str | None) -> str | None:
    value = entity(text)
    if value == "USER":
        value = user_name or ""
    if not re.fullmatch(NAME, value) or re.search(
        r"\b(?:and|or|someone|somebody|him|her|them|he|she|they)\b", value, re.I,
    ):
        return None
    return value


@dataclass(frozen=True)
class Fact:
    subject: str
    relation: str
    object: str
    memory_type: str = "FACT"
    aliases: tuple[str, ...] = ()

    def key(self, scope: str) -> str:
        # Aliases are subjects: one alias can have only one active target.
        return f"{scope}:{self.subject.casefold()}:{self.relation}"


@dataclass
class Interpretation:
    facts: list[Fact] = field(default_factory=list)
    status: str = "UNRECOGNIZED"
    correction: bool = False


def interpret(text: str, *, explicit: bool = False, user_name: str | None = None) -> Interpretation:
    if not isinstance(text, str) or not text.strip():
        return Interpretation(status="REJECTED")
    value = clean(text)
    value = re.sub(r"^(?:hey\s+)?tars[, :]+", "", value, flags=re.I)
    marked = bool(PREFIX.match(value))
    value = PREFIX.sub("", value)
    correction = bool(CORRECTION.match(value))
    value = CORRECTION.sub("", value)
    # Also accept "Actually, remember that ...".
    marked = marked or bool(PREFIX.match(value))
    value = PREFIX.sub("", value)
    requested = explicit or marked or correction or bool(re.match(
        r"(?:my\s+(?:name|default|preferred|(?:current\s+)?project)\b|"
        r"your\s+(?:developer|creator|maker)\b|"
        r"i\s+(?:built|created|developed|made|use|prefer|work|am working)\b|call\b)", value, re.I))
    if len(text) > MAX_TEXT:
        return Interpretation(status="REJECTED" if requested else "UNRECOGNIZED")
    if SECRET.search(value):
        if not requested and re.match(r"(?:what|how|why|explain|describe)\b", value, re.I):
            return Interpretation()
        return Interpretation(status="REJECTED")
    if TRANSIENT.search(value):
        return Interpretation(status="EPHEMERAL" if requested else "UNRECOGNIZED")
    if UNCERTAIN.search(value):
        return Interpretation(status="NEEDS_CLARIFICATION" if requested else "UNRECOGNIZED")
    if not requested:
        return Interpretation()
    facts: list[Fact] = []
    # Resolve "I built you. My name is Nikhil" without guessing the user's name.
    name_match = re.search(rf"\bmy name is ({NAME})(?:\.|$)", value, re.I)
    if name_match:
        user_name = clean(name_match[1])
    clauses = re.split(r"[.!]\s+|;\s*", value)
    for clause in clauses:
        clause = clean(clause)
        fact: Fact | None = None
        if m := re.fullmatch(rf"{ASSISTANT}\s+(?:were|was|got)\s+{DEVELOPER}\s+by\s+(.+)", clause, re.I):
            if creator := developer(m[1], user_name):
                fact = Fact("TARS", "developed_by", creator)
        elif m := re.fullmatch(rf"(.+?)\s+{DEVELOPER}\s+{ASSISTANT}", clause, re.I):
            if creator := developer(m[1], user_name):
                fact = Fact("TARS", "developed_by", creator)
        elif m := re.fullmatch(r"(?:your|tars'?s?|this assistant's)\s+(?:developer|creator|maker)\s+is\s+(.+)", clause, re.I):
            if creator := developer(m[1], user_name):
                fact = Fact("TARS", "developed_by", creator)
        elif m := re.fullmatch(rf"my name is ({NAME})", clause, re.I):
            fact = Fact("USER", "called", clean(m[1]))
        elif m := re.fullmatch(r"my (?:preferred (?:chart )?|default (?:chart )?)(?:timeframe|time frame) is (.+)", clause, re.I):
            if tf := timeframe(m[1]):
                fact = Fact("USER", "default_timeframe", tf, "PREFERENCE")
        elif m := re.fullmatch(r"my default (?:chart )?(?:symbol|instrument) is (.+)", clause, re.I):
            if target := symbol(m[1]):
                fact = Fact("USER", "default_symbol", target, "PREFERENCE")
        elif m := re.fullmatch(r"when i say (.+?) i mean (.+)", clause, re.I):
            if target := symbol(m[2]):
                alias = clean(m[1]).lower()
                if re.fullmatch(r"[\w -]{1,40}", alias):
                    fact = Fact(alias, "alias_of", target, "ALIAS", (alias,))
        elif m := re.fullmatch(r"call (.+?) (\w+)", clause, re.I):
            if target := symbol(m[1]):
                alias = m[2].lower()
                fact = Fact(alias, "alias_of", target, "ALIAS", (alias,))
        elif m := re.fullmatch(r"i (?:am working|work) on (.{1,120})", clause, re.I):
            fact = Fact("USER", "works_on", clean(m[1]), "PROJECT_CONTEXT")
        elif m := re.fullmatch(r"(?:my project is|my current project is) (.{1,120})", clause, re.I):
            fact = Fact("USER", "works_on", clean(m[1]), "PROJECT_CONTEXT")
        elif m := re.fullmatch(r"i use (.{1,120})", clause, re.I):
            fact = Fact("USER", "uses", clean(m[1]), "PREFERENCE")
        elif m := re.fullmatch(r"i prefer (.{1,120})", clause, re.I):
            fact = Fact("USER", "prefers", clean(m[1]), "PREFERENCE")
        elif m := re.fullmatch(r"i risk (\d+(?:\.\d+)?%) per trade", clause, re.I):
            fact = Fact("USER", "prefers", f"risk {m[1]} per trade", "PREFERENCE")
        if fact is None:
            # Never persist a partly understood compound statement.
            return Interpretation(status="NEEDS_CLARIFICATION", correction=correction)
        facts.append(fact)
    return Interpretation(facts, "READY", correction)


def recall_target(query: str) -> tuple[str, str] | None:
    value = clean(query).rstrip("?")
    value = re.sub(r"^(?:can you tell me|tell me|do you remember|remind me)[, ]+", "", value, flags=re.I)
    if re.fullmatch(rf"who (?:{DEVELOPER}) {ASSISTANT}", value, re.I) or re.fullmatch(
        r"who(?:'s| is) (?:your|tars'?s?|this assistant's) (?:developer|creator|maker)", value, re.I
    ) or re.fullmatch(
        rf"who (?:was|were) {ASSISTANT} {DEVELOPER} by|by whom (?:was|were) {ASSISTANT} {DEVELOPER}", value, re.I
    ) or re.fullmatch(
        r"what(?:'s| is) (?:your|tars'?s?|this assistant's) (?:developer|creator|maker)'s name", value, re.I
    ):
        return "TARS", "developed_by"
    if re.fullmatch(r"(?:what(?:'s| is) my name|who am i)", value, re.I):
        return "USER", "called"
    if re.fullmatch(r"(?:what(?:'s| is) my (?:default|preferred)(?: chart)? (?:timeframe|time frame)|"
                    r"which (?:timeframe|time frame) do i (?:prefer|use))", value, re.I):
        return "USER", "default_timeframe"
    if re.fullmatch(r"what(?:'s| is) my default (?:chart )?(?:symbol|instrument)", value, re.I):
        return "USER", "default_symbol"
    if re.fullmatch(r"what (?:am i working|do i work) on|what(?:'s| is) my (?:current )?project", value, re.I):
        return "USER", "works_on"
    if re.fullmatch(r"what do i (?:prefer|use)", value, re.I):
        return "USER", "uses" if value.lower().endswith("use") else "prefers"
    return None


def answer(fact: dict) -> str:
    value, relation = fact["object"], fact["relation"]
    return {
        "developed_by": f"I was developed by {value}.",
        "called": f"Your name is {value}.",
        "default_timeframe": f"Your default chart timeframe is {value}.",
        "default_symbol": f"Your default symbol is {value}.",
        "alias_of": f"{fact['subject']} means {value}.",
        "works_on": f"You're working on {value}.",
        "uses": f"You use {value}.",
        "prefers": f"You prefer {value}.",
    }[relation]
