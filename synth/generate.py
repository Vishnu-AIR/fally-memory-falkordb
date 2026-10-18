"""Synthetic data generator: fake users, fake meetings, chats, mails, calendar events and promises.

Everything here is invented. Each user is a persona; each persona's life is a list of "episodes" that the generator
turns into extractor-style graph blocks (the same shape Fally's extractor writes), with dates spread over the weeks
before a base date. Names come from fixed fictional pools so the demo is reproducible.

  python -m synth.generate            # print all blocks as JSON
  python -m scripts.seed              # load them into FalkorDB (one graph per user)
"""

from __future__ import annotations

import hashlib
import json
import random
from datetime import datetime, timedelta, timezone

BASE = datetime(2026, 10, 16, 10, 0, tzinfo=timezone.utc)


def _key(kind: str, value: str) -> str:
    """A fake strong key (e.g. an email hash) so resolution has something to match on."""
    return f"{kind}:{hashlib.sha256(value.encode()).hexdigest()[:12]}"


def _ts(days_ago: float, rnd: random.Random) -> str:
    d = BASE - timedelta(days=days_ago, minutes=rnd.randint(0, 600))
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


class Episode:
    """One source (a meeting, a chat window, a mail, a calendar event) and what the extractor found in it."""

    def __init__(self, user: str, n: int, kind: str, title: str, days_ago: float, rnd: random.Random):
        self.source = {"type": kind, "id": f"{user}-{kind}-{n:03d}", "ts": _ts(days_ago, rnd), "title": title}
        self.nodes: list[dict] = []
        self.edges: list[dict] = []

    def node(self, tmp, type, name, quote=None, keys=None, aliases=None, props=None):
        self.nodes.append({"tmp": tmp, "type": type, "name": name, "quote": quote, "keys": keys or [],
                           "aliases": aliases or [], "props": props or {}})
        return self

    def edge(self, src, dst, type, quote=None, confidence=0.8, basis="stated", valid_from=None):
        self.edges.append({"src": src, "dst": dst, "type": type, "quote": quote, "confidence": confidence,
                           "basis": basis, "valid_from": valid_from})
        return self

    def promise(self, tmp, owner, recipient, text, due, quote, key):
        self.node(tmp, "commitment", text, quote=quote, keys=[key], props={"due": due, "status": "open"})
        self.edge(owner, tmp, "promised", quote=quote, confidence=0.9)
        self.edge(tmp, recipient, "owed_by", quote=quote, confidence=0.9)
        return self

    def block(self) -> dict:
        return {"source": self.source, "graph": {"nodes": self.nodes, "edges": self.edges}}


def asha(rnd: random.Random) -> tuple[dict, list[dict]]:
    u = "asha"
    tomas = dict(type="person", name="Tomas Reyes", keys=[_key("email", "tomas@lumen.example")])
    eps = []
    e = Episode(u, 1, "meeting", "Atlas pricing review", 30, rnd)
    e.node("t", quote="Tomas: I'll own the pricing model for Atlas.", **tomas)
    e.node("lumen", "organisation", "Lumen Labs", quote="everyone at Lumen Labs is heads down on Atlas")
    e.node("atlas", "project", "Atlas launch", quote="the Atlas launch is set for November")
    e.node("ines", "person", "Ines Duarte", quote="Ines: the onboarding screens are ready for review",
           keys=[_key("email", "ines@lumen.example")])
    e.edge("t", "lumen", "works_at", "Tomas, who runs pricing at Lumen Labs", valid_from="2025-03-01")
    e.edge("ines", "lumen", "works_at", "Ines from the Lumen Labs design team", valid_from="2024-06-01")
    e.edge("self", "atlas", "works_on", "I'm leading the Atlas launch")
    e.edge("ines", "atlas", "contributes_to", "Ines: the onboarding screens are ready for review")
    e.edge("self", "t", "works_with", "Tomas and I sync every Tuesday on Atlas")
    e.promise("p1", "self", "t", "Send Tomas the Atlas pricing deck", "2026-10-20",
              "Me: I'll send you the pricing deck before the 20th.", "commitment:asha-meeting-001:1")
    eps.append(e)

    e = Episode(u, 2, "mail", "Q4 roadmap request", 21, rnd)
    e.node("nadia", "person", "Nadia Kim", quote="Nadia: can you get me the Q4 roadmap draft?",
           keys=[_key("email", "nadia@lumen.example")], props={"label": "manager"})
    e.node("lumen", "organisation", "Lumen Labs")
    e.edge("self", "nadia", "reports_to", "Nadia, my manager", valid_from="2025-01-10")
    e.edge("nadia", "lumen", "works_at", "Nadia Kim, VP Product, Lumen Labs")
    e.promise("p2", "self", "nadia", "Share the Q4 roadmap draft with Nadia", "2026-10-24",
              "Me: I'll have the Q4 roadmap draft to you next week.", "commitment:asha-mail-002:1")
    eps.append(e)

    e = Episode(u, 3, "meeting", "Coffee with Tomas", 9, rnd)
    e.node("t", quote="Tomas: I'm joining Northwind Bikes next month.", **tomas)
    e.node("nw", "organisation", "Northwind Bikes", quote="Northwind Bikes offered him the head of pricing role")
    e.node("kofi", "person", "Kofi Mensah", quote="Tomas: you should meet Kofi Mensah, he built Northwind's app")
    e.edge("t", "nw", "works_at", "Tomas: I start at Northwind Bikes on the 1st.", confidence=0.9,
           valid_from="2026-10-01")
    e.edge("kofi", "nw", "works_at", "Kofi built Northwind's app")
    e.promise("p3", "t", "self", "Tomas to introduce Asha to Kofi Mensah", "2026-10-22",
              "Tomas: I'll intro you to Kofi this week, promise.", "commitment:asha-meeting-003:1")
    eps.append(e)

    e = Episode(u, 4, "whatsapp", "Chat with Rohan", 6, rnd)
    e.node("r", "person", "Rohan", quote="Rohan: are we still on for the climbing gym Saturday?")
    e.node("gym", "place", "Boulder Hub Pune", quote="the climbing gym on FC Road")
    e.edge("r", "gym", "likes", "Rohan: Boulder Hub is my second home")
    eps.append(e)

    e = Episode(u, 5, "calendar", "Lumen offsite", 14, rnd)
    e.node("rm", "person", "Rohan Mehta", keys=[_key("email", "rohan.mehta@lumen.example")],
           quote="Attendees: Rohan Mehta, Ines Duarte, Asha Varma")
    e.node("ines", "person", "Ines Duarte", keys=[_key("email", "ines@lumen.example")])
    e.node("off", "event", "Lumen offsite 2026", quote="Lumen offsite, Goa, 2 Oct")
    e.edge("self", "off", "attended", "Asha Varma accepted: Lumen offsite")
    e.edge("rm", "off", "attended", "Rohan Mehta accepted")
    e.edge("ines", "off", "attended", "Ines Duarte accepted")
    e.edge("rm", "ines", "works_with", "Rohan Mehta and Ines Duarte, onboarding squad")
    eps.append(e)

    e = Episode(u, 6, "whatsapp", "Chat with Jonah", 3, rnd)
    e.node("j", "person", "Jonah Pike", quote="Jonah: date night Friday? I booked the rooftop place",
           props={"label": "boyfriend"})
    e.node("pune", "place", "Pune")
    e.edge("self", "j", "knows", "my boyfriend Jonah booked the rooftop place for date night", confidence=0.95)
    e.edge("self", "pune", "lives_in", "back home in Pune", valid_from="2024-01-01")
    e.promise("p4", "self", "j", "Pick up Jonah's camera from the repair shop", "2026-10-19",
              "Me: I'll grab your camera from the repair shop on Monday.", "commitment:asha-whatsapp-006:1")
    eps.append(e)

    e = Episode(u, 7, "meeting", "Atlas weekly", 2, rnd)
    e.node("ines", "person", "Ines Duarte", keys=[_key("email", "ines@lumen.example")],
           quote="Ines: pricing page copy is blocked on the deck")
    e.node("atlas", "project", "Atlas launch")
    e.node("pricing", "topic", "Atlas pricing", quote="we spent the hour on Atlas pricing tiers")
    e.edge("atlas", "pricing", "about", "we spent the hour on Atlas pricing tiers")
    e.edge("self", "ines", "works_with", "Ines and I paired on the pricing page")
    eps.append(e)
    return {"id": u, "name": "Asha Varma", "about": "Product lead at Lumen Labs (fictional)"}, [x.block() for x in eps]


def dev(rnd: random.Random) -> tuple[dict, list[dict]]:
    u = "dev"
    eps = []
    e = Episode(u, 1, "meeting", "Seed round call", 25, rnd)
    e.node("mira", "person", "Mira Solberg", keys=[_key("email", "mira@fjord.example")],
           quote="Mira: Fjord Ventures likes the repeat-order numbers")
    e.node("fjord", "organisation", "Fjord Ventures", quote="Fjord Ventures leads seed rounds in food tech")
    e.node("orbit", "organisation", "Orbit Coffee", quote="Orbit Coffee ships 2,000 bags a month")
    e.edge("mira", "fjord", "works_at", "Mira Solberg, partner at Fjord Ventures")
    e.edge("self", "orbit", "works_at", "I started Orbit Coffee in 2024", valid_from="2024-02-01")
    e.promise("p1", "self", "mira", "Send Mira the monthly investor update", "2026-11-01",
              "Me: you'll get the monthly update on the first.", "commitment:dev-meeting-001:1")
    e.promise("p2", "mira", "self", "Mira to send the term sheet draft", "2026-10-23",
              "Mira: I'll get you a term sheet draft by Friday.", "commitment:dev-meeting-001:2")
    eps.append(e)

    e = Episode(u, 2, "conversation", "Ops sync with Oren", 18, rnd)
    e.node("oren", "person", "Oren Blum", keys=[_key("phone", "+10000000001")],
           quote="Oren: the roaster in Austin is booked till December")
    e.node("orbit", "organisation", "Orbit Coffee")
    e.node("austin", "place", "Austin")
    e.edge("oren", "orbit", "works_at", "Oren, my co-founder at Orbit Coffee", valid_from="2024-02-01")
    e.edge("oren", "austin", "lives_in", "Oren lives near the Austin roastery", valid_from="2023-05-01")
    e.edge("self", "oren", "works_with", "Oren and I split ops and sales")
    eps.append(e)

    e = Episode(u, 3, "whatsapp", "Family chat", 12, rnd)
    e.node("zara", "person", "Zara Malhotra", quote="Zara: mom says come home for Diwali",
           props={"label": "sister"})
    e.edge("self", "zara", "knows", "my sister Zara asked about Diwali")
    e.promise("p3", "self", "zara", "Book Diwali train tickets for the family", "2026-10-25",
              "Me: I'll book the train tickets tonight.", "commitment:dev-whatsapp-003:1")
    eps.append(e)

    e = Episode(u, 4, "conversation", "Oren moving", 5, rnd)
    e.node("oren", "person", "Oren Blum", keys=[_key("phone", "+10000000001")],
           quote="Oren: we closed on the Denver flat")
    e.node("denver", "place", "Denver", quote="Denver, closer to the new roaster")
    e.edge("oren", "denver", "lives_in", "Oren: we moved to Denver last week", confidence=0.9,
           valid_from="2026-10-05")
    eps.append(e)

    e = Episode(u, 5, "mail", "Green bean order", 4, rnd)
    e.node("kofi", "person", "Kofi Mensah", keys=[_key("email", "kofi@beanroute.example")],
           quote="Kofi: the Ethiopia lot ships on the 28th")
    e.node("bean", "organisation", "Bean Route Imports")
    e.node("tomas", "person", "Tomas Reyes", keys=[_key("email", "tomas@beanroute.example")],
           quote="cc Tomas Reyes, logistics")
    e.edge("kofi", "bean", "works_at", "Kofi Mensah, Bean Route Imports")
    e.edge("tomas", "bean", "works_at", "Tomas Reyes, logistics, Bean Route Imports")
    e.promise("p4", "kofi", "self", "Kofi to confirm the Ethiopia lot shipping date", "2026-10-21",
              "Kofi: I'll confirm the ship date by Wednesday.", "commitment:dev-mail-005:1")
    eps.append(e)
    return {"id": u, "name": "Dev Malhotra", "about": "Founder of Orbit Coffee (fictional)"}, [x.block() for x in eps]


def lina(rnd: random.Random) -> tuple[dict, list[dict]]:
    u = "lina"
    eps = []
    e = Episode(u, 1, "meeting", "Bluefin kickoff", 20, rnd)
    e.node("pablo", "person", "Pablo Ortiz", keys=[_key("email", "pablo@bluefin.example")],
           quote="Pablo: we need the patient app redesign before the board meeting")
    e.node("blue", "organisation", "Bluefin Health")
    e.node("app", "project", "Bluefin patient app")
    e.edge("pablo", "blue", "works_at", "Pablo Ortiz, product director at Bluefin Health")
    e.edge("self", "app", "works_on", "I'm redesigning the Bluefin patient app")
    e.promise("p1", "self", "pablo", "Deliver first patient app mockups to Pablo", "2026-10-22",
              "Me: first mockups by next Thursday.", "commitment:lina-meeting-001:1")
    eps.append(e)

    e = Episode(u, 2, "whatsapp", "Studio chat", 8, rnd)
    e.node("hana", "person", "Hana Sato", quote="Hana: I can do the illustrations for Bluefin")
    e.node("ezra", "person", "Ezra Cole", quote="Ezra: studio rent is due on the 1st")
    e.node("studio", "place", "Canal Street Studio")
    e.edge("hana", "ezra", "knows", "Hana and Ezra share the studio")
    e.edge("hana", "studio", "uses", "Hana works out of Canal Street Studio")
    e.edge("ezra", "studio", "owns", "Ezra holds the studio lease")
    e.edge("self", "hana", "collaborates_with", "Hana is doing the Bluefin illustrations with me")
    e.promise("p2", "hana", "self", "Hana to send illustration sketches", "2026-10-19",
              "Hana: sketches by Monday!", "commitment:lina-whatsapp-002:1")
    eps.append(e)
    return {"id": u, "name": "Lina Okafor", "about": "Freelance product designer (fictional)"}, [x.block() for x in eps]


PERSONAS = [asha, dev, lina]


def generate(seed: int = 7) -> list[tuple[dict, list[dict]]]:
    rnd = random.Random(seed)
    out = []
    for p in PERSONAS:
        user, blocks = p(rnd)
        blocks.sort(key=lambda b: b["source"]["ts"])  # ingest in the order things happened
        out.append((user, blocks))
    return out


if __name__ == "__main__":
    print(json.dumps([{"user": u, "blocks": b} for u, b in generate()], indent=2))
