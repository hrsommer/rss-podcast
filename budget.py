"""
Kostenbremsen: Monatsbudget und Höchstzahl an Läufen pro Tag.

Jeder Lauf, der Claude aufruft, wird mit seinen tatsächlichen Kosten in site/costs.json
eingetragen. Vor dem nächsten Aufruf wird geprüft:
  - Wurden heute schon zu viele Läufe gemacht?
  - Würde dieser Lauf das Monatsbudget überschreiten?
Greift eine Bremse, bricht das Programm mit einer Fehlermeldung ab. Der GitHub-Lauf
schlägt dann fehl, und GitHub schickt automatisch eine E-Mail.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

KEEP_DAYS = 70  # ältere Einträge werden gelöscht, für das Monatsbudget reichen gut zwei Monate


class BudgetExceeded(Exception):
    pass


def load(site: Path) -> list[dict]:
    path = site / "costs.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def runs_today(runs: list[dict], today: datetime) -> int:
    return sum(1 for r in runs if r["date"] == today.date().isoformat())


def month_total(runs: list[dict], today: datetime) -> float:
    month = today.strftime("%Y-%m")
    return sum(r["cost_usd"] for r in runs if r["date"].startswith(month))


def check(runs: list[dict], today: datetime, worst_case_usd: float, limits: dict) -> None:
    """Wirft BudgetExceeded, wenn eine der Bremsen greift."""
    if worst_case_usd > limits["max_cost_per_run_usd"]:
        raise BudgetExceeded(
            f"Dieser Lauf könnte bis zu {worst_case_usd:.2f} $ kosten, erlaubt sind "
            f"{limits['max_cost_per_run_usd']:.2f} $ pro Lauf (limits.max_cost_per_run_usd).")

    done = runs_today(runs, today)
    if done >= limits["max_runs_per_day"]:
        raise BudgetExceeded(
            f"Heute gab es schon {done} Läufe mit Claude, erlaubt sind {limits['max_runs_per_day']} "
            "(limits.max_runs_per_day). Morgen geht es automatisch weiter.")

    spent = month_total(runs, today)
    if spent + worst_case_usd > limits["monthly_budget_usd"]:
        raise BudgetExceeded(
            f"Monatsbudget erreicht: bisher {spent:.2f} $ ausgegeben, dieser Lauf könnte bis zu "
            f"{worst_case_usd:.2f} $ kosten, das Budget ist {limits['monthly_budget_usd']:.2f} $ "
            "(limits.monthly_budget_usd). Ab dem 1. des nächsten Monats geht es weiter.")


def record(site: Path, runs: list[dict], today: datetime, cost_usd: float, note: str) -> list[dict]:
    """Trägt die tatsächlichen Kosten eines Laufs ein und speichert die Liste sofort."""
    runs = runs + [{
        "date": today.date().isoformat(),
        "time": today.strftime("%H:%M"),
        "cost_usd": round(cost_usd, 4),
        "note": note,
    }]
    cutoff = (today - timedelta(days=KEEP_DAYS)).date().isoformat()
    runs = [r for r in runs if r["date"] >= cutoff]
    site.mkdir(parents=True, exist_ok=True)
    (site / "costs.json").write_text(json.dumps(runs, indent=2, ensure_ascii=False), encoding="utf-8")
    return runs
