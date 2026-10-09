#!/usr/bin/env python3
"""Автоматический парсер цен ВкусВилл для анализа инфляции.

Читает config.yaml, тянет цены с карточек товаров, пересчитывает цену за
чистый 1 кг / 1 л, проверяет стоимость корзины на 1/4 МРОТ Москвы, дописывает
строку в food_inflation.csv и пушит её в origin/main.

Зависимости: requests, beautifulsoup4, pyyaml, pandas, rich (rich опционален).
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import requests
import yaml
from bs4 import BeautifulSoup

try:
    import pandas as pd
except ImportError:  # pragma: no cover - pandas уже в requirements.txt
    pd = None

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich import box as rich_box

    RICH = True
except ImportError:  # pragma: no cover - fallback path
    RICH = False

BASE_DIR = Path(__file__).resolve().parent
CONFIG_NAME = "config.yaml"
CSV_NAME = "food_inflation.csv"
CSV_SEP = ";"
DATE_COL = "Дата"
INFLATION_COL = "Инфляция, %"

# Размер упаковки: «1 л», «900 г», «1000 мл», «1,3 кг». Порядок альтернатив
# важен: «кг» раньше «г», «мл» раньше «л».
SIZE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(кг|г|мл|л)\b", re.IGNORECASE)
_MARKUP_RE = re.compile(
    r"\[(?:/?)(?:bold|red|green|yellow|cyan|magenta|blue|dim|white|"
    r"bright_red|bright_green|bright_yellow|bright_cyan|bright_white)\]"
)

console = None


# -------------------------------------------------------------------- консоль


def configure_stdio() -> None:
    """Переводит вывод в UTF-8.

    Консоль Windows по умолчанию живёт в cp866/cp1251, где нет ни «Δ», ни
    символов рамок rich. Без этого любой юникод-символ роняет рендеринг.
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


class _PlainConsole:
    """Минимальная замена rich.console.Console, если rich не установлен."""

    def print(self, text="", **_kwargs):
        print(_MARKUP_RE.sub("", str(text)))


def build_console():
    global console
    if console is not None:
        return console
    console = Console(highlight=False) if RICH else _PlainConsole()
    return console


def out(text="", **_kwargs):
    build_console().print(text, **_kwargs)


# ------------------------------------------------------------------- config


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    if not cfg.get("ITEMS"):
        raise ValueError(f"{path.name}: пустой или отсутствующий список ITEMS")
    return cfg


def item_names(cfg: dict) -> list[str]:
    return [str(it["Группа"]) for it in cfg["ITEMS"]]


def price_col(item: str) -> str:
    return f"{item} цена"


def source_col(item: str) -> str:
    return f"{item} источник"


# ------------------------------------------------------------------ разбор HTML


def fetch_html(session: requests.Session, url: str, cfg: dict) -> str:
    headers = {
        "User-Agent": cfg.get("User_Agent", "Mozilla/5.0"),
        "Accept-Language": "ru-RU,ru;q=0.9",
    }
    timeout = cfg.get("Таймаут_сек", 30)
    resp = session.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def parse_price(soup: BeautifulSoup) -> tuple[float | None, str | None]:
    """Возвращает (цена, название) из JSON-LD Product, с regex-запасным планом."""
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        for obj in data if isinstance(data, list) else [data]:
            if not isinstance(obj, dict) or obj.get("@type") != "Product":
                continue
            name = obj.get("name")
            offers = obj.get("offers")
            if isinstance(offers, list):
                offers = offers[0] if offers else None
            if isinstance(offers, dict) and offers.get("price") is not None:
                price = _to_float(offers["price"])
                if price is not None:
                    return price, name
    # Запасной вариант: инлайн-JSON вида "price":93
    m = re.search(r'"price"\s*:\s*"?(\d+(?:[.,]\d+)?)', soup.get_text())
    if m:
        return _to_float(m.group(1)), None
    return None, None


def extract_package(soup: BeautifulSoup, name: str | None) -> tuple[float, str] | None:
    """Размер упаковки — из блока «Вес/объем», иначе из названия товара."""
    for node in soup.find_all(["h2", "h3", "h4", "h5"]):
        if node.get_text(strip=True).lower().startswith("вес/объ"):
            parent = node.find_parent(
                class_=re.compile("DetailProdPageInfoDescItem")
            ) or node.parent
            desc = parent.find(class_=re.compile("__Desc")) if parent else None
            text = (desc or parent).get_text(" ", strip=True)
            m = SIZE_RE.search(text)
            if m:
                return _to_float(m.group(1)), m.group(2).lower()
    if name:
        m = SIZE_RE.search(name.replace("\xa0", " "))
        if m:
            return _to_float(m.group(1)), m.group(2).lower()
    return None


def to_base(qty: float, unit: str) -> tuple[float, str]:
    """Приводит упаковку к кг или л (г→кг, мл→л)."""
    unit = unit.lower()
    if unit == "кг":
        return qty, "кг"
    if unit in ("г", "гр"):
        return qty / 1000.0, "кг"
    if unit == "л":
        return qty, "л"
    if unit in ("мл", "ml"):
        return qty / 1000.0, "л"
    return qty, unit


def price_per_unit(
    price: float, package: tuple[float, str] | None, target: str
) -> tuple[float, bool]:
    """Цена за 1 target (кг/л). Второй элемент — была ли пересчитана одна валюта
    измерения в другую (кг↔л, плотность ≈ 1)."""
    if package is None:
        # Товары на развес (картофель, яблоки): цена на странице уже за кг.
        return price, False
    qty, unit = to_base(*package)
    cross = False
    if unit != target:
        if {unit, target} == {"кг", "л"}:
            cross = True  # жидкость: 1 кг ≈ 1 л
        else:
            return price, False
    if qty <= 0:
        return price, cross
    return price / qty, cross


def _to_float(raw) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip().replace(" ", "").replace("\xa0", "")
    if not text:
        return None
    text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


# ------------------------------------------------------------------------ CSV


def csv_path() -> Path:
    return BASE_DIR / CSV_NAME


def csv_header(names: list[str]) -> list[str]:
    path = csv_path()
    if path.exists() and path.stat().st_size > 0:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            first = fh.readline().rstrip("\r\n")
        return next(csv.reader([first], delimiter=CSV_SEP))
    cols = [DATE_COL]
    for name in names:
        cols += [price_col(name), source_col(name)]
    cols.append(INFLATION_COL)
    return cols


def read_history():
    """История как список словарей. Первая строка — база для инфляции."""
    path = csv_path()
    if not path.exists() or path.stat().st_size == 0:
        return []
    if pd is not None:
        df = pd.read_csv(path, sep=CSV_SEP, encoding="utf-8-sig", dtype=str)
        return df.to_dict("records")
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh, delimiter=CSV_SEP))


def append_row(row: dict[str, str], names: list[str]) -> None:
    path = csv_path()
    exists = path.exists() and path.stat().st_size > 0
    header = csv_header(names)
    # utf-8-sig пишет BOM в начало каждого потока, поэтому при дозаписи в
    # существующий файл используем utf-8, иначе BOM попадёт в середину.
    with path.open("a" if exists else "w", encoding="utf-8" if exists else "utf-8-sig",
                   newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=header, delimiter=CSV_SEP)
        if not exists:
            writer.writeheader()
        writer.writerow({col: row.get(col, "") for col in header})


# ------------------------------------------------------------------ вычисления


def compute_inflation(
    base: dict[str, str] | None, current: dict[str, float], names: list[str]
) -> float | None:
    """Среднее изменение цен корзины, % к первой строке CSV (базе)."""
    if not base:
        return None
    deltas = []
    for name in names:
        old = _to_float(base.get(price_col(name)))
        new = current.get(name)
        if old and old > 0 and new is not None:
            deltas.append((new - old) / old * 100.0)
    if not deltas:
        return None
    return sum(deltas) / len(deltas)


def check_mrot(total: float, cfg: dict) -> tuple[bool, float, float, float]:
    mrot = float(cfg["МРОТ_Москва_2026"])
    tol = float(cfg.get("Допуск_процентов", 5)) / 100.0
    target = mrot / 4.0
    return target * (1 - tol) <= total <= target * (1 + tol), target, target * (1 - tol), target * (1 + tol)


# --------------------------------------------------------------------- отчёт


def fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:,.2f}".replace(",", " ")


def show_report(results: list[dict], total: float, inflation: float | None, cfg: dict) -> None:
    table = Table(title="Корзина ВкусВилл", box=rich_box.ROUNDED, header_style="bold cyan")
    table.add_column("Категория", style="dim")
    table.add_column("Продукт", style="bold")
    table.add_column("Цена", justify="right")
    table.add_column("Упаковка", justify="right")
    table.add_column("За 1 ед.", justify="right")
    table.add_column("Источник", overflow="fold", style="dim")
    for r in results:
        pkg = "—" if not r["package"] else f'{r["package"][0]:g} {r["package"][1]}'
        cost = "—" if r["cost"] is None else fmt(r["cost"])
        price = "—" if r["price"] is None else fmt(r["price"])
        table.add_row(r["category"], r["name"], price, pkg, cost, r["url"])
    out()
    out(table)

    ok, target, lo, hi = check_mrot(total, cfg)
    style = "green" if ok else "bold yellow"
    out(
        f"[{style}]Сумма корзины: {fmt(total)} руб. "
        f"(1/4 МРОТ = {fmt(target)}; допустимо {fmt(lo)}…{fmt(hi)})[/{style}]"
    )
    if not ok:
        out(
            "[yellow]Внимание: корзина выходит за пределы ±"
            f"{cfg.get('Допуск_процентов', 5)}% от 1/4 МРОТ Москвы.[/yellow]"
        )
    if inflation is None:
        out("[dim]Инфляция: первая строка — база.[/dim]")
    else:
        color = "red" if inflation > 0.005 else ("green" if inflation < -0.005 else "dim")
        out(f"[{color}]Инфляция к базе: {inflation:+.2f}%[/{color}]")


# ------------------------------------------------------------------------ git


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def git_sync(message: str = "Auto-update inflation data") -> bool:
    out("[dim]Git: добавление и коммит…[/dim]")
    # «git add .» забирает весь проект (скрипт, config.yaml, requirements.txt,
    # AGENTS.md, CSV). .venv/ и __pycache__/ отсекает .gitignore.
    add = git("add", ".")
    if add.returncode != 0:
        out(f"[red]git add не удался:[/red] {add.stderr.strip()}")
        return False

    status = git("status", "--porcelain")
    if status.returncode == 0 and not status.stdout.strip():
        out("[dim]Изменений нет — коммит и push пропущены.[/dim]")
        return True

    commit = git("commit", "-m", message)
    if commit.returncode != 0:
        out(f"[red]git commit не удался:[/red] {commit.stderr.strip()}")
        return False

    out("[green]Коммит создан.[/green]")
    push = git("push", "origin", "main")
    if push.returncode != 0:
        out(f"[red]git push не удался:[/red] {push.stderr.strip()}")
        return False

    out("[bold green]Запушено в origin/main.[/bold green]")
    return True


# ------------------------------------------------------------------------ main


def main() -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="Парсер цен ВкусВилл для анализа инфляции")
    parser.add_argument("--config", default=CONFIG_NAME, help="путь к config.yaml")
    parser.add_argument("--no-git", action="store_true", help="не коммитить и не пушить")
    args = parser.parse_args()

    if not RICH:
        out("[yellow]rich не установлен — вывод будет упрощённым.[/yellow]")

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = BASE_DIR / config_path
    cfg = load_config(config_path)
    names = item_names(cfg)

    if RICH:
        out(Panel(f"[bold]Парсинг цен[/bold] · {date.today().isoformat()}", border_style="cyan"))

    history = read_history()
    base = history[0] if history else None

    session = requests.Session()
    results: list[dict] = []
    current: dict[str, float] = {}
    for item in cfg["ITEMS"]:
        name = str(item["Группа"])
        target = str(item.get("Мера_измерения", "кг")).lower()
        url = str(item["Ссылка"])
        record = {
            "category": str(item.get("Категория", "")),
            "name": name,
            "url": url,
            "price": None,
            "package": None,
            "cost": None,
        }
        try:
            html = fetch_html(session, url, cfg)
            soup = BeautifulSoup(html, "html.parser")
            price, title = parse_price(soup)
            if price is None:
                raise ValueError("цена не найдена на странице")
            package = extract_package(soup, title or name)
            cost, cross = price_per_unit(price, package, target)
            record.update(price=price, package=package, cost=cost)
            current[name] = cost
            if cross:
                out(f"[dim]{name}: упаковка в другой мере, 1 кг ≈ 1 л.[/dim]")
        except Exception as exc:  # noqa: BLE001 - сеть/разметка/разбор
            out(f"[red]{name}: не удалось получить цену — {exc}[/red]")
        results.append(record)

    total = sum(r["cost"] for r in results if r["cost"] is not None)
    inflation = compute_inflation(base, current, names)
    show_report(results, total, inflation, cfg)

    missing = [r["name"] for r in results if r["cost"] is None]
    if missing:
        out(f"[yellow]Без цены: {', '.join(missing)}[/yellow]")

    row: dict[str, str] = {DATE_COL: date.today().strftime("%d.%m.%Y")}
    for item in cfg["ITEMS"]:
        name = str(item["Группа"])
        cost = next((r["cost"] for r in results if r["name"] == name), None)
        row[price_col(name)] = "" if cost is None else f"{cost:.2f}"
        row[source_col(name)] = str(item["Ссылка"])
    row[INFLATION_COL] = "" if inflation is None else f"{inflation:.2f}"

    try:
        append_row(row, names)
    except OSError as exc:
        out(f"[red]Не удалось записать {CSV_NAME}:[/red] {exc}")
        return 1

    out(f"[green]Записано в {CSV_NAME}[/green]")

    if args.no_git:
        out("[dim]--no-git: пропускаю коммит и push.[/dim]")
        return 0

    out()
    return 0 if git_sync() else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        out("\n[yellow]Прервано пользователем — ничего не записано.[/yellow]")
        sys.exit(130)
