# AGENTS.md

## What this is

`inflation_parser.py` scrapes weekly ВкусВилл prices for a fixed 10-item basket
(defined in `config.yaml`), recalculates each price to a clean 1 kg / 1 L, checks
the basket against 1/4 of Moscow's 2026 МРОТ, appends a row to
`food_inflation.csv`, and pushes that CSV + `config.yaml` to `origin/main`.

There is **no** package layout, test suite, linter, or CI. The only deps are
pinned in `requirements.txt` (`requests`, `beautifulsoup4`, `PyYAML`, `pandas`,
`rich`); `rich` is optional (plain-text fallback) but the rest are required.

## Commands

```powershell
& ".\.venv\Scripts\python.exe" inflation_parser.py          # normal run (auto-pushes)
& ".\.venv\Scripts\python.exe" inflation_parser.py --no-git  # write CSV, skip git
& ".\.venv\Scripts\python.exe" inflation_parser.py --config other.yaml
```

`--no-git` is the flag for testing — without it every run commits and pushes.
There are no prompts; the run is fully non-interactive.

## Scraping contract (the non-obvious part)

- Product pages are server-rendered: the price is the JSON-LD
  `Product.offers.price` value, so `requests` + `BeautifulSoup` works (no JS
  engine / browser needed). Send a real browser `User-Agent` (see `config.yaml`).
- Package size comes from the «Вес/объем» block (`…DetailProdPageInfoDescItem__Desc`),
  falling back to a `\d+ (кг|г|л|мл)` match in the product title.
- By-weight goods (Картофель, Яблоки) have **no** package block and no size in
  the title: the page price is already per kg, so `extract_package` returns
  `None` and the price is used as-is.
- Кефир's page states `1000 г` while the basket item is `1 л`; mass↔volume is
  treated 1:1 (density ≈ 1) for liquids. Same idea applies to any кг↔л mismatch.
- A basket of one unit each currently totals ~1900–2000 руб — far below 1/4 МРОТ
  (9932.50). The "outside ±5 %" warning is therefore **normal**, not a bug.

## Data files

`config.yaml` — `Магазин`, `МРОТ_Москва_2026`, `Допуск_процентов`, `User_Agent`,
and `ITEMS` (each with `Категория`, `Группа`, `Мера_измерения` ∈ {кг, л}, `Ссылка`).
The `Группа` string is the schema key: it must match the `… цена` / `… источник`
columns in the CSV. Renaming or reordering `ITEMS` is a CSV schema change.

`food_inflation.csv` — **append-only history**, one row per run.
- Separator `;`, encoding `utf-8-sig`. Keep both — it's the user's dataset.
- 22 columns: `Дата` (DD.MM.YYYY), then `цена` + `источник` per item, then
  `Инфляция, %`. New runs append in this exact layout (header is read from the
  existing file when present).
- `Инфляция, %` is the **mean** per-item % change vs the **first** row (base),
  blank on the first row.
- Gotcha: `utf-8-sig` re-emits a BOM on every write stream, so rows appended to
  an existing file are written as plain `utf-8`; only a freshly created file
  gets the BOM.

## Git state

Branch `main`, remote `origin` -> `https://github.com/nikitashulga1234567-cloud/Products`.
The binding commands (`init`, `branch -M main`, `remote add origin`) are already
done; re-running `git remote add origin` fails with `remote origin already exists`.

The script's `git_sync` runs `git add .` (whole project — script, `config.yaml`,
`requirements.txt`, `AGENTS.md`, CSV) and commits with `Auto-update inflation
data`, so any working-tree change gets pushed. `.gitignore` keeps `.venv/` and
`__pycache__/` out. A run with no staged diff skips commit+push. `git
add/commit/push` failures are reported, not raised.

## Windows / shell gotchas

- Shell is **Windows PowerShell 5.1** — `&&` does not work, chain with
  `cmd1; if ($?) { cmd2 }`.
- The console is cp1251/cp866 and cannot encode `Δ`, `≈` (U+2248), or rich's box
  characters. `configure_stdio()` re-encodes stdout/stderr to UTF-8 to prevent a
  crash — keep it intact if you add Unicode output.
- Captured output from the Cyrillic repo path (`...\Desktop\Продукты`) shows
  mojibake and garbled paths. Cosmetic, not a bug.
- `core.autocrlf=true` globally; checked-out text files get CRLF endings.
- Python 3.14 in `.venv` is the only interpreter; don't add a second venv. The
  script resolves its config/CSV from **its own directory**, not the cwd.
