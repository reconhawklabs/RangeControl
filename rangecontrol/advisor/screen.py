"""Stage three: screen a ruling's public reply before anyone reads it.

The ruling model holds the whole range and writes the one piece of prose the
blue team reads. Its prompt forbids disclosure; this module checks the prompt
held. Two layers:

- ``check_reply`` is deterministic. It catches the common shapes of a
  disclosure: claims about what does or does not exist, exercise vocabulary,
  and addresses or range terms the asker never used. It is a floor, not the
  defence. Red-teaming showed most paraphrases pass it ("that's the backup
  server's address" contains no tell-tale phrase at all).
- ``review_reply`` asks a model whether the reply tells the asker anything
  about their own network. That judgement is the real second layer. Like the
  gate, the reviewing model is never given the range document, so it has
  nothing it could leak in turn.

Either layer flagging means the reply is withheld and ``fallback_reply`` is
sent in its place. The white cell still sees the original.
"""

from __future__ import annotations

import functools
import json
import random
import re

from rangecontrol.advisor.gate import parse_json_object
from rangecontrol.advisor.prompts import (
    REPLY_SCREEN_SCHEMA,
    REPLY_SCREEN_SYSTEM_PROMPT,
    SCREENED_APPROVALS,
    SCREENED_DENIALS,
)
from rangecontrol.llm.base import EFFORT_MEDIUM, LLMError, Provider
from rangecontrol.range_doc.loader import load_template

# Same reasoning as GATE_MAX_TOKENS: the cap covers thinking plus the reply,
# and a truncation here would withhold every ruling's text.
REVIEW_MAX_TOKENS = 4000

# Phrasings that tell the asker what their network does or does not contain.
# Each was checked against a corpus of benign cautions (tests/
# test_reply_screen.py) and narrowed where it tripped one: a false positive
# costs a generic reply in place of the model's, and the more often that
# happens the more a generic reply starts to mean something.
_DISCLOSURE = tuple(
    re.compile(pattern)
    for pattern in (
        # Inventory, diagrams, records
        r"\binventor(?:y|ies)\b",
        r"\bassets?\b",
        r"\bcmdb\b",
        r"\bdiagrams?\b",
        r"\b(?:host|asset|server|device) ?lists?\b",
        r"\btopolog(?:y|ies)\b",
        r"\bon (?:our|the|any) books\b",
        r"\b(?:no|any|our) records?\b",
        r"\b(?:not|never|nothing|isn't|aren't) on (?:record|file)\b",
        # Existence and status
        r"\bexist(?:s|ed|ence)?\b",
        (
            r"(?<!change )\b(?:is|are|isn't|aren't|not|never|been|was|were) "
            r"(?:present|deployed|tracked|listed|recogni[sz]ed|known|unused|"
            r"in use)\b"
        ),
        r"\b(?:unknown|unrecogni[sz]ed|unmanaged|unassigned|unallocated)\b",
        r"\b(?:decommission(?:ed|ing)?|dormant|orphaned|honeypots?)\b",
        r"\b(?:harmless|moot)\b",
        r"\bin use anywhere\b",
        (
            r"\bthere(?: is|'s| are) (?:no|not any|nothing|none)\b"
            r"(?! (?:need|reason|rush|harm|change freeze|freeze)\b)"
        ),
        r"\bthere (?:isn't|aren't|is not|are not)\b(?! (?:a|any) (?:need|reason)\b)",
        r"\bno (?:such|known|matching)\b",
        r"\bno \w+ (?:here|anywhere)\b",
        r"\bno \w+ on (?:the|your|our|this) (?:network|segment|vlan|subnet)\b",
        r"\bnothing (?:in|on|we|that|here|matching|like|at)\b",
        r"\b(?:is|are) empty\b",
        # Ownership and counts
        r"\b(?:one )?of ours\b",
        r"\bone we (?:use|run|own|have)\b",
        r"\b(?:owned by|belongs? to)\b",
        r"\b(?:is|are|actually|really) (?:yours|ours)\b",
        (
            r"\bwe (?:don't|do not|never|didn't|did not|can't|cannot|couldn't) "
            r"(?:have|run|use|host|operate|own|deploy|see|find|know|track|"
            r"recogni[sz]e)\b"
        ),
        r"\bwe have no\b",
        r"\b(?:our|the|your) only\b",
        r"\bwe only (?:have|run)\b",
        r"\bboth (?:dcs|hosts|servers|domain controllers|accounts|firewalls)\b",
        # What a change would or wouldn't touch
        (
            r"\b(?:won't|will not|wouldn't|would not|doesn't|does not|"
            r"shouldn't|should not) (?:touch|affect|impact|hit|reach|break) "
            r"(?:any|anything|nothing|our|prod(?:uction)?)\b"
        ),
        r"\bno (?:impact|effect)\b",
        (
            r"\b(?:nobody|no one|nothing) (?:depends|relies|uses|listens|runs|"
            r"needs|matches)\b"
        ),
        r"\b(?:hooked up|connected|attached) to (?:anything|nothing)\b",
        r"\bwon't find anything\b",
        (
            r"\b(?:associated|correspond(?:s|ing)?|relat(?:ed|es)|maps?) "
            r"(?:with|to) (?:anything|nothing|any|that|this|it)\b"
        ),
        r"\b(?:relies|rely|depends|depend) on (?:that|this|the|it)\b",
        (
            r"\b(?:our|any of our) (?:systems?|environment|network|estate|side|"
            r"vlans?|subnets?|segments?|hosts?|servers?|machines?|devices?)\b"
        ),
        r"\b(?:any|every) (?:vlan|subnet|segment)s?\b",
        r"\bwe(?: are|'re)? (?:not )?(?:aware|tracking|seeing)\b",
        # Scope
        r"\bout[- ]of[- ]scope\b|\b(?:in|outside(?: of)?) scope\b",
        r"\boff[- ]limits\b|\bdo not touch\b",
        # Second-guessing why they asked
        r"\bwhat (?:led|prompted|brought) you\b",
        r"\b(?:on|onto) your radar\b",
        r"\bcaught your eye\b",
        r"\bwhere you got\b",
        r"\bwhy you (?:flagged|raised|picked|chose|asked)\b",
        r"\b(?:actually|really) (?:there|present|exists?|in use|out there)\b",
    )
)

# Words that place the asker inside an exercise.
_EXERCISE = re.compile(
    r"\b(?:exercises?(?! (?:caution|care|judge?ment|discretion|restraint))|"
    r"scenarios?|injects?|msel|red[- ]?team(?:ers?)?|adversar(?:y|ies|ial)|"
    r"white[- ]?(?:cell|team)|opfor|attackers?|scoring|scoreboard|callbacks?|"
    r"beacons?|staging host|planned activit(?:y|ies)|range control|"
    r"scheduled events?|on purpose|intentional(?:ly)?)\b"
)

_IPV4 = re.compile(r"(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?(?![\w/]|\.\d)")
_IPV6 = re.compile(
    r"(?<![\w:])(?:(?:[0-9a-f]{1,4}:){3,7}[0-9a-f]{1,4}"
    r"|(?:[0-9a-f]{1,4}:){1,6}:(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4})*)?)(?![\w:])"
)
# "10.77.0.x", "the 10.77 range": a subnet named without being written out.
_PARTIAL = re.compile(
    r"(?<![\w.])\d{1,3}\.\d{1,3}(?:\.\d{1,3})?\.[x*](?!\w)"
    r"|(?<![\w.])\d{1,3}\.\d{1,3}(?:\.\d{1,3})? (?:range|subnet|block|network|segment)\b"
)

_TOKEN = re.compile(r"[A-Za-z0-9](?:[\w.-]*[A-Za-z0-9])?")
_WORD = re.compile(r"[A-Za-z][A-Za-z']*")
_SEPARATORS = re.compile(r"[-_.]")
_CAMEL = re.compile(r"[a-z][A-Z]")
_BACKTICKED = re.compile(r"`([^`\n]{3,64})`")
_SENTENCE_START = re.compile(r"(?:^|[.!?:|#>\-*]\s*|\n\s*)$")

# Protocol and tooling acronyms common enough to appear in any caution
# ("keep your SSH session open"). Not range-specific, so not range terms;
# a reply that names one suspiciously is the reviewer's to judge.
_GENERIC_TERMS = frozenset(
    """
    acl ad api arp cpu dhcp dmz dns ftp http https icmp id ip ldap ldaps lan
    mfa nat ntp os ram rdp sftp smb snmp ssh ssl sso tcp tls udp url vlan vpn
    wan and not the
    """.split()
)

# Invisible characters and defanging ("10.77.0[.]10") that would otherwise
# split a term or an address in two without changing what the reader sees.
_ZERO_WIDTH = re.compile("[​‌‍⁠﻿­]")
_DEFANG = re.compile(r"\[\.\]|\(\.\)|\{\.\}")
_MARKDOWN = re.compile(r"[*_`~|]+")


def _clean(text: str) -> str:
    """Normalise without losing identifiers: underscores and case survive."""
    text = _ZERO_WIDTH.sub("", text)
    text = _DEFANG.sub(".", text)
    return text.replace("’", "'").replace("‘", "'")


def _prose(text: str) -> str:
    """Lowercased, markdown stripped, whitespace collapsed: for phrase matching.

    Discord renders "there are **no** printers" and a reply broken across
    lines exactly like the plain sentence, so the patterns must see it that way.
    """
    return " ".join(_MARKDOWN.sub("", _clean(text)).lower().split())


def _identifier_like(token: str) -> bool:
    """Shaped like a hostname, account, or tag rather than an English word.

    ``DC-VULCAN``, ``fw-edge``, ``svc_backup``, ``WS01``, ``VulcanDC`` — but
    not ``Approved``, ``e.g``, or a version number like ``2.4``.
    """
    if len(token) < 3 or not any(c.isalpha() for c in token):
        return False
    if _SEPARATORS.search(token):
        return len(token) >= 4
    if token.isalpha():
        return token.isupper() or bool(_CAMEL.search(token))
    return True  # letters and digits: WS01


def _parts(token: str) -> tuple[str, ...]:
    return (token, *_SEPARATORS.split(token))


def _proper_nouns(text: str) -> set[str]:
    """Capitalised words that never appear in lowercase anywhere in ``text``
    and are not just starting a sentence, table cell, or heading: names like
    ``Vulcan`` or ``Mercury``, as opposed to ``Server`` in "Server segment"."""
    lowercase = {w for w in _WORD.findall(text) if w.islower()}
    found: set[str] = set()
    for match in _WORD.finditer(text):
        word = match.group(0)
        if len(word) < 3 or not word[0].isupper() or not word[1:].islower():
            continue
        if word.lower() in lowercase:
            continue
        if _SENTENCE_START.search(text[max(0, match.start() - 4) : match.start()]):
            continue
        found.add(word)
    return found


def _terms(text: str) -> set[str]:
    """Identifier-shaped terms in ``text`` and their identifier-shaped parts,
    lowercased: ``DC-VULCAN`` yields ``dc-vulcan`` and ``vulcan``."""
    found = {
        part.lower()
        for token in _TOKEN.findall(text)
        for part in _parts(token)
        if _identifier_like(part)
    }
    found |= {span.strip().lower() for span in _BACKTICKED.findall(text)}
    return found


@functools.lru_cache(maxsize=1)
def _template_words() -> frozenset[str]:
    """Every word of the shipped range template: structure, not range data."""
    try:
        template = load_template()
    except FileNotFoundError:
        return frozenset()
    words = {w.lower() for w in _WORD.findall(template)}
    return frozenset(words | _terms(template))


def range_vocabulary(*texts: str) -> frozenset[str]:
    """Every range-specific term in the range material, built once."""
    vocabulary: set[str] = set()
    for text in texts:
        cleaned = _clean(text)
        vocabulary |= _terms(cleaned)
        vocabulary |= {word.lower() for word in _proper_nouns(cleaned)}
    return frozenset(vocabulary - _template_words() - _GENERIC_TERMS)


def _words_of(text: str) -> set[str]:
    """Every word and identifier in ``text``, and their parts, lowercased.

    The echo check compares against this rather than the raw question: as a
    substring test, an asker who wrote "alphabet.example" would license a
    reply that says "ALPHA".
    """
    return {
        part.lower()
        for token in _TOKEN.findall(text)
        for part in _parts(token)
        if part
    }


def _addresses(text: str) -> set[str]:
    lowered = text.lower()
    found = {m.removesuffix("/32") for m in _IPV4.findall(lowered)}
    found |= set(_IPV6.findall(lowered))
    found |= {m for m in _PARTIAL.findall(lowered)}
    return found


def check_reply(reply: str, question: str, vocabulary: frozenset[str]) -> str:
    """Return why ``reply`` must be withheld, or "" when it may be sent.

    Anything the asker said first may be echoed back: their own words are not
    a disclosure.
    """
    text, asked = _prose(reply), _prose(question)

    claims = sorted(
        {m.group(0) for p in _DISCLOSURE for m in p.finditer(text)}
        - {m.group(0) for p in _DISCLOSURE for m in p.finditer(asked)}
    )
    if claims:
        return f"says what is or isn't on the network: {', '.join(claims)}"

    exercise = sorted(
        {m.group(0) for m in _EXERCISE.finditer(text)}
        - {m.group(0) for m in _EXERCISE.finditer(asked)}
    )
    if exercise:
        return f"mentions the exercise: {', '.join(exercise)}"

    # Any address, not only the range's: an invented one reads as a hint too.
    addresses = sorted(_addresses(_clean(reply)) - _addresses(_clean(question)))
    if addresses:
        return f"names an address the asker did not: {', '.join(addresses)}"

    said = _words_of(_clean(question))
    terms = sorted((_words_of(_clean(reply)) & vocabulary) - said)
    if terms:
        return f"names a range term the asker did not: {', '.join(terms)}"
    return ""


def review_reply(reply: str, question: str, provider: Provider) -> str:
    """Ask a context-free model whether ``reply`` discloses anything.

    Returns the reason when it does, "" when it does not. Raises LLMError on
    unusable output; the caller withholds the reply in that case.
    """
    # JSON, not labelled sections: the request is asker-controlled, and a
    # question ending "\n\nReply:\nApproved." would otherwise forge a second,
    # harmless-looking reply for the reviewer to judge instead of the real one.
    payload = json.dumps(
        {"request": question.strip(), "reply": reply.strip()}, ensure_ascii=False
    )
    raw = provider.complete(
        system=REPLY_SCREEN_SYSTEM_PROMPT,
        user=payload,
        schema=REPLY_SCREEN_SCHEMA,
        max_tokens=REVIEW_MAX_TOKENS,
        effort=EFFORT_MEDIUM,
    )
    verdict = parse_json_object(raw)

    # Strictly a bool. The string "false" is truthy; guessing at intent is how
    # a disclosure gets waved through.
    discloses = verdict.get("discloses")
    if not isinstance(discloses, bool):
        raise LLMError(f"reply review returned a non-boolean verdict: {discloses!r}")
    if not discloses:
        return ""

    reason = verdict.get("reason")
    reason = reason.strip() if isinstance(reason, str) else ""
    return reason or "reviewer flagged the reply without giving a reason"


def fallback_reply(verdict: str, rng: random.Random) -> str:
    """A generic reply for ``verdict``, drawn at random so none becomes a tell."""
    pool = SCREENED_APPROVALS if verdict == "APPROVED" else SCREENED_DENIALS
    return rng.choice(pool)
