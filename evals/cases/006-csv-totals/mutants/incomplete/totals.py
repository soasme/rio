import csv
import io
from decimal import Decimal, InvalidOperation


def totals(text: str) -> dict:
    result = {}
    for row in csv.DictReader(io.StringIO(text)):
        try:
            amount = Decimal(str(float(row["amount"])))
        except InvalidOperation as exc:
            raise ValueError("invalid amount") from exc
        result[row["customer"]] = result.get(row["customer"], Decimal(0)) + amount
    return result
