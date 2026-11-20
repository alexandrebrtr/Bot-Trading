"""Parsing XML défensif pour les flux externes (RSS Fed, Trésor US).

Les DTD et entités sont refusées (protection contre l'expansion d'entités) et
la taille est bornée. Les noms d'éléments sont lus sans espace de noms.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

MAX_XML_BYTES = 20 * 1024 * 1024


class UnsafeXMLError(ValueError):
    pass


def parse_xml(raw: bytes) -> ET.Element:
    if len(raw) > MAX_XML_BYTES:
        raise UnsafeXMLError("Document XML trop volumineux")
    head = raw[:4096].upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in raw.upper():
        raise UnsafeXMLError("DTD / entités refusées")
    return ET.fromstring(raw)


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def child_text(el: ET.Element, name: str) -> str | None:
    for c in el:
        if local(c.tag) == name:
            return c.text
    return None
