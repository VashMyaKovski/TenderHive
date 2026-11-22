import re
import unicodedata

from modelgw.errors import Failure, GatewayError

_REDACTIONS = (
    (
        "PERSONAL",
        re.compile(
            r"(?i)(?:\b(?:ф\.?\s*и\.?\s*о\.?|фио|паспорт|личный номер|телефон|email|"
            r"адрес(?: проживания)?|full name|passport|phone|address)\s*[:=]\s*[^\n;]+)"
            r"|(?<![\w.+-])[\w.+-]+@[\w.-]+\.[a-zа-я]{2,}"
            r"|(?<!\w)(?:\+?375[\s().-]*|8[\s().-]*0[\s().-]*)"
            r"(?:\d[\s().-]*){8}\d(?!\d)"
            r"|\b[A-ZА-Я]{2}\d{7}\b"
            r"|\b\d{7}[A-ZА-Я]\d{3}(?:[A-ZА-Я]{2}\d|[A-ZА-Я]\d{2})\b"
        ),
    ),
    (
        "ORGANIZATION",
        re.compile(
            r"(?i)\b(?:юр(?:идический)?\.? адрес|наименование организации|organization)"
            r"\s*[:=]\s*[^\n;]+|\b(?:ООО|ОДО|ОАО|ЗАО|УП|ИП|LLC|LTD)\s*[«\"]?[^\n;,.»\"]+[»\"]?"
        ),
    ),
    (
        "BANKING",
        re.compile(
            r"(?i)\b(?:iban|swift|bic|бик|расч[её]тный сч[её]т|банковский сч[её]т)"
            r"\s*[:=]\s*[^\n;]+|\bBY\d{2}(?:\s*[A-Z0-9]){24}\b"
            r"|(?<!\d)(?:\d[ -]?){12,19}\d(?!\d)"
        ),
    ),
    (
        "REGISTRATION",
        re.compile(
            r"(?i)\b(?:унп|инн|огрн|регистрационный номер|tax id|registration number)"
            r"\s*[:=]\s*[^\n;]+|(?<!\w)\d{9}(?!\w)"
        ),
    ),
    (
        "COMMERCIAL",
        re.compile(
            r"(?i)\b(?:цена|стоимость|сумма|себестоимость|скидка|маржа|маржинальность|штраф|"
            r"price|cost|margin|discount|budget|penalty)\s*[:=]\s*[^\n;]+"
            r"|\b\d[\d ,.]*\s*(?:BYN|USD|EUR|RUB|бел\.?\s*руб\.?|руб\.?)(?!\w)"
        ),
    ),
)

_UNCLASSIFIED = re.compile(
    r"(?i)\b(?:[a-zа-я]{2}\s*\d{7}|(?:[a-z]{2}\d{2})(?:[ -]*[a-z\d]){16,30})\b"
    r"|\b(?:фио|паспорт|унп|iban|swift|личный номер|расч[её]тный сч[её]т)\s+(?=\w)"
    r"|\b(?:цена|маржа|скидка|бюджет|штраф|price|margin|discount|budget|penalty)"
    r"[^\n;0-9]{0,24}\d",
)


def redact_public_text(text: str) -> str:
    # Normalization makes lookalike spacing and format controls ineffective as separators.
    normalized = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
    normalized = "".join(
        char
        for char in normalized
        if unicodedata.category(char) not in {"Cf", "Cc"} or char == "\n" or char == "\t"
    )
    for category, pattern in _REDACTIONS:
        normalized = pattern.sub(f"[REDACTED_{category}]", normalized)
    if _UNCLASSIFIED.search(normalized) or any(
        pattern.search(normalized) for _, pattern in _REDACTIONS
    ):
        raise GatewayError(Failure.UNSAFE_CONTEXT)
    if not normalized.strip():
        raise GatewayError(Failure.UNSAFE_CONTEXT)
    return normalized.strip()
